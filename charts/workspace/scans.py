"""Security scans — the pure half (#726).

A scan is a long-running job that produces a stream of findings. kube-coder
already has that shape twice (`boards/runs.py` for lifecycle, the Hypervisor for
normalising a CLI's output), so this module composes both rather than inventing
a third, and keeps the same purity split the boards package uses: everything
here is a function over plain dicts, and the impure half — threads, the
filesystem, the event bus — lives in `ScansManager` below the fold in this same
file, taking its collaborators by injection.

## Why the scanner never appears above `scan_backends.ScanBackend`

The maintainer's condition for this feature was one seam: moving the scanner off
the pod later must be a backend swap, not a rewrite. So nothing in this module
knows what runs a scan, where it runs, or what address it reaches. It is handed
parsed artifacts and returns parsed state. Grep this file for "strix" and the
only hits are in prose explaining a format we read.

## Why findings are passed through unchanged

A finding is the scanner's own record — its wording, its severity, its
reproduction steps. Re-shaping it into a model of our own would mean inventing
severities and summaries that the tool did not assert, in the one place where
inventing anything is indefensible. So a finding travels verbatim, and the only
fields this module reads are `id` (identity, for diffing) and `severity`
(ordering and counts). Everything else is opaque payload. That also makes the
parser robust to a scanner version that adds fields.

A user's *disposition* of a finding (open / dismissed) is ours, not the
scanner's, so it is stored beside the findings rather than inside them.
"""

import ipaddress
import os
import re
import shutil
import sys
import threading
import time
import traceback
import uuid

# The one atomic-JSON-document implementation in the tree: flock sidecar plus
# tmp/rename, with a compare-and-set `update`. Imported on its own rather than
# through the boards package, exactly as the trigger ledger does — `store` is
# stdlib-only, so a scan record must not become unavailable because some other
# module in that package grew a dependency this workspace lacks.
import boards.store


# ── lifecycle ──────────────────────────────────────────────────────────────

#: `failed` is deliberately distinct from `done`. A scan that could not reach
#: the app — bad credentials, no runtime, target refused the connection —
#: produces an empty findings list, which is byte-identical to a clean result.
#: Collapsing the two would render "nothing found" over a scan that never
#: looked, which is the single worst failure mode a security surface has.
#: `interrupted` is distinct from `stopped` for the reason boards/runs.py gives:
#: stopped means a human ended it, interrupted means the process died under it,
#: and hiding a crash behind an ordinary-looking status is issue #462 again.
SCAN_STATUSES = ('running', 'done', 'stopped', 'failed', 'interrupted')
LIVE_SCAN_STATUSES = ('running',)
TERMINAL_SCAN_STATUSES = ('done', 'stopped', 'failed', 'interrupted')

#: How a backend's own run status maps onto ours. Anything unrecognised is
#: treated as `failed` rather than guessed at — see `map_backend_status`.
BACKEND_STATUS_MAP = {
    'running': 'running',
    'completed': 'done',
    'stopped': 'stopped',
    'failed': 'failed',
    'interrupted': 'interrupted',
}

#: Scan depth. The value is passed to the backend verbatim; the estimate is
#: shown in the picker, because how long it takes IS part of the choice.
SCAN_MODES = ('quick', 'standard', 'deep')
MODE_ESTIMATES = {
    'quick': 'about 5 minutes',
    'standard': '30 to 60 minutes',
    'deep': '1 to 4 hours',
}
#: Never inherit the scanner's own default, which is `deep` — a casually
#: started scan must not be a multi-hour bill.
DEFAULT_SCAN_MODE = 'quick'

#: Severity order for display. A finding whose severity the scanner reports as
#: something not in this list sorts last and counts under `other`, rather than
#: being dropped or coerced into a level nobody asserted.
SEVERITIES = ('critical', 'high', 'medium', 'low')

#: Bounds on what a caller may submit. These are sanity limits, not policy:
#: the model string is whatever the scanner accepts, so we check only that it
#: is one plausible line of text.
MAX_MODEL_LEN = 200
MAX_INSTRUCTION_LEN = 4000
MAX_BUDGET_USD = 1000.0

_SCAN_ID_RE = re.compile(r'^scn_[a-z0-9]{12}$')
#: A model identifier is `provider/model`, possibly with further slashes
#: (`openrouter/vendor/model`). Control characters are refused because the
#: value is written into a config file and echoed back into the UI.
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]')


def new_scan_id():
    return 'scn_' + uuid.uuid4().hex[:12]


def valid_scan_id(scan_id):
    """Gate before any path is built from a caller-supplied id."""
    return bool(_SCAN_ID_RE.match(scan_id or ''))


def is_terminal(status):
    return status in TERMINAL_SCAN_STATUSES


def map_backend_status(raw):
    """A backend's run status → ours. Unknown maps to `failed`.

    Mapping an unrecognised status to `done` would be the dangerous direction:
    a scanner that grows a new terminal state we have never seen would start
    reporting clean results. `failed` is the safe unknown — it says "this did
    not complete normally", which is true of every case we cannot classify.
    """
    return BACKEND_STATUS_MAP.get((raw or '').strip().lower(), 'failed')


# ── validation ─────────────────────────────────────────────────────────────

def validate_create(body, targets, *, connected_model=''):
    """Check a create request. Returns `(spec, error)`; exactly one is None.

    Every rejection here happens before the backend is touched, which is the
    point: a scan costs the user real money in their own LLM account, so a
    request that cannot succeed must fail while it is still free.

    `targets` is the list of currently scannable apps (see `scannable_targets`)
    — the port is checked against live state rather than trusted, so a scan can
    never be pointed at a port nothing is serving, nor at one the scanner
    cannot reach. The second is refused here and not only greyed out in a UI:
    it is the scan that runs, spends, and reports a clean result against
    nothing.
    """
    if not isinstance(body, dict):
        return None, 'Invalid request body.'

    model = (body.get('model') or connected_model or '').strip()
    if not model:
        return None, ('No model is configured. Connect a model before '
                      'starting a scan.')
    if len(model) > MAX_MODEL_LEN or _CONTROL_RE.search(model):
        return None, 'That model name is not valid.'

    port = body.get('port')
    if not isinstance(port, int) or isinstance(port, bool):
        return None, 'Pick an app to scan.'
    target = next((t for t in targets if t.get('port') == port), None)
    if target is None:
        return None, (f'Nothing is running on port {port}. Start the app '
                      f'first, then scan it.')
    if not target.get('reachable', True):
        return None, (target.get('reason') or
                      'The scanner cannot reach this app, so a scan would '
                      'check nothing.')

    mode = (body.get('mode') or DEFAULT_SCAN_MODE).strip().lower()
    if mode not in SCAN_MODES:
        return None, f'Scan depth must be one of: {", ".join(SCAN_MODES)}.'

    # Optional, exactly as the scanner has it — but the UI always shows the
    # field, and warns when it is empty, because an uncapped deep scan is an
    # open-ended bill in the user's own account.
    budget = body.get('budget_usd')
    if budget in (None, ''):
        budget = None
    else:
        try:
            budget = float(budget)
        except (TypeError, ValueError):
            return None, 'Budget must be a number, or empty for no cap.'
        if budget <= 0 or budget > MAX_BUDGET_USD:
            return None, f'Budget must be between 0 and {MAX_BUDGET_USD:g}.'

    instruction = (body.get('instruction') or '').strip()
    if len(instruction) > MAX_INSTRUCTION_LEN:
        return None, (f'Extra instructions must be under '
                      f'{MAX_INSTRUCTION_LEN} characters.')

    return {
        'port': port,
        'name': target.get('name') or '',
        'mode': mode,
        'model': model,
        'budget_usd': budget,
        'instruction': instruction,
    }, None


def _is_loopback_addr(addr):
    """True when `addr` only accepts connections from inside this namespace.

    Not a `127.` prefix test. AppsManager canonicalises a v4-mapped listener
    to `::ffff:127.0.0.1` (a JVM that binds 127.0.0.1 on an AF_INET6 socket
    routinely produces exactly that), which no prefix check matches, so the
    app was offered as scannable and the scan ended "found nothing" having
    reached nothing at all -- the precise outcome this warning exists to stop.
    Parsing the address instead of pattern-matching it covers that form, the
    `::ffff:7f00:1` hex spelling, and 127.x/::1, without a list to maintain.
    """
    text = (addr or '').strip()
    if not text:
        return False
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        # Not an address we can parse (a hostname, or a format we have not
        # seen). Fall back to the literal checks rather than guessing.
        return text.startswith('127.') or text == '::1'
    return (ip.ipv4_mapped or ip if ip.version == 6 else ip).is_loopback


def scannable_targets(apps, internal_ports=()):
    """The apps a scan may point at, derived from the Applications list.

    Only what is listening right now: a stopped app would produce a scan that
    reaches nothing and reports it as clean. `reachable` carries the one
    precondition users hit and cannot diagnose — an app bound to loopback only
    is invisible from outside the workspace's own network namespace, so the
    scanner connects to nothing and finds nothing, with no error anywhere.
    The bind address is already in the apps list, so the warning costs nothing
    and lands before the money is spent.
    """
    out = []
    for app in apps or []:
        if app.get('status') != 'running':
            continue
        port = app.get('port')
        if not isinstance(port, int) or port in tuple(internal_ports):
            continue
        addr = app.get('addr') or ''
        loopback_only = _is_loopback_addr(addr)
        out.append({
            'port': port,
            'name': app.get('name') or '',
            'addr': addr,
            'reachable': not loopback_only,
            'reason': (
                f'This app is bound to {addr}, which only accepts connections '
                f'from inside the workspace itself. Restart it listening on '
                f'0.0.0.0 so the scanner can reach it.'
                if loopback_only else ''
            ),
        })
    out.sort(key=lambda t: t['port'])
    return out


# ── reading what the backend produced ──────────────────────────────────────

def parse_findings(raw):
    """Normalise the backend's findings payload to a list of dicts.

    Pass-through by design (see the module docstring): the only guarantees made
    are that the result is a list, that every entry is a dict, and that every
    entry has a non-empty string `id` — because identity is what diffing,
    dispositions and deep links are keyed on. An entry without one cannot be
    referred to later, so it is dropped rather than given a synthetic id that
    would change between reads.
    """
    if not isinstance(raw, list):
        return []
    out = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        fid = item.get('id')
        if not isinstance(fid, str) or not fid.strip() or fid in seen:
            continue
        seen.add(fid)
        out.append(item)
    return out


def severity_of(finding):
    """A finding's severity, lower-cased, or '' when it asserts none."""
    value = (finding or {}).get('severity')
    value = value.strip().lower() if isinstance(value, str) else ''
    return value if value in SEVERITIES else ''


def severity_rank(finding):
    """Sort key: most severe first, then whatever the scanner said last.

    An unrecognised or absent severity sorts after every known one rather than
    being hidden — it is still a finding somebody has to read.
    """
    sev = severity_of(finding)
    return SEVERITIES.index(sev) if sev else len(SEVERITIES)


def sort_findings(findings):
    """Severity first, then newest-reported first within a severity.

    Two stable passes rather than one tuple key: severity sorts ascending
    while the timestamp sorts descending, and a tuple cannot express both
    directions over a string. The single key this replaced read
    `(severity_rank, ts)` ascending, i.e. oldest-first -- the opposite of
    the documented order, with no test pinning it either way.
    """
    def recency(f):
        return str(f.get('timestamp') or f.get('updated_at') or '')
    return sorted(sorted(findings or [], key=recency, reverse=True),
                  key=severity_rank)


def count_severities(findings):
    """`{critical, high, medium, low, other, total}` over a findings list."""
    counts = {s: 0 for s in SEVERITIES}
    counts['other'] = 0
    for f in findings or []:
        sev = severity_of(f)
        counts[sev if sev else 'other'] += 1
    counts['total'] = sum(counts[k] for k in (*SEVERITIES, 'other'))
    return counts


def highest_severity(findings):
    """The most severe level present, or '' — what a notification leads with."""
    for sev in SEVERITIES:
        if any(severity_of(f) == sev for f in findings or []):
            return sev
    return ''


def diff_findings(previous, current):
    """`(added, changed)` between two reads of the findings list.

    Keyed on `id`, because a scanner revises a finding in place as it gathers
    evidence — severity moves, a reproduction appears — and the UI has to tell
    "a new problem" from "more detail about a problem you have seen". Equality
    is a whole-record comparison rather than a revision counter, so a scanner
    that does not keep one still produces correct change events.
    """
    before = {f['id']: f for f in parse_findings(previous)}
    added, changed = [], []
    for f in parse_findings(current):
        old = before.get(f['id'])
        if old is None:
            added.append(f)
        elif old != f:
            changed.append(f)
    return added, changed


def parse_report(raw):
    """The scanner's write-up as `{'text', 'location'}`, or None.

    Passed through unchanged, for the same reason findings are (see the
    module docstring): it is the scanner's own account of what it did, and
    rewriting it here would mean asserting things the tool did not. Only the
    shape is checked, because both fields go on a page.
    """
    if not isinstance(raw, dict):
        return None
    text = raw.get('text')
    if not isinstance(text, str) or not text.strip():
        return None
    location = raw.get('location')
    return {'text': text,
            'location': location if isinstance(location, str) else ''}


def parse_usage(raw):
    """Spend and token counters from a backend's run record.

    Defensive about types because these numbers are rendered live: a provider
    that reports a cost as a string must not blank the whole panel.
    """
    raw = raw if isinstance(raw, dict) else {}

    def num(key, cast):
        try:
            return cast(raw.get(key) or 0)
        except (TypeError, ValueError):
            return cast(0)

    return {
        'requests': num('requests', int),
        'input_tokens': num('input_tokens', int),
        'output_tokens': num('output_tokens', int),
        'total_tokens': num('total_tokens', int),
        'cost_usd': num('cost', float),
    }


def apply_artifacts(record, artifacts):
    """Fold one read of the backend's artifacts into a scan record in place.

    Returns `(added, changed)` findings so the caller can emit precise events.
    The record's status is only ever moved FORWARD out of `running`: once a
    scan is terminal a late artifact read must not resurrect it, which is what
    would otherwise happen when a stop request and a final write race.
    """
    artifacts = artifacts or {}
    run = artifacts.get('run') if isinstance(artifacts.get('run'), dict) else {}

    # A backend reports `None` for "I could not read the findings this time",
    # which is not the same as "there are none". Keeping what we already hold
    # means a bad read costs one poll of freshness instead of blanking the
    # list and re-announcing every finding once it reads cleanly again.
    reported_findings = artifacts.get('findings')
    if reported_findings is None:
        added, changed = [], []
        findings = parse_findings(record.get('findings'))
    else:
        findings = parse_findings(reported_findings)
        added, changed = diff_findings(record.get('findings'), findings)
    record['findings'] = findings
    record['counts'] = count_severities(findings)
    # Same reasoning as the findings above, for the same reason: an empty
    # `run` is what a mid-rewrite read of run.json looks like, and that read
    # happens on every confirmed finding. Overwriting unconditionally flickers
    # the live spend to $0.00 -- and makes it permanent for any scan that ends
    # through the `not alive` branch, which never re-reads a good run.json.
    reported_usage = run.get('llm_usage')
    if reported_usage is not None:
        record['usage'] = parse_usage(reported_usage)
    elif not record.get('usage'):
        record['usage'] = parse_usage(None)

    # The write-up and the scanner's own coverage verdict, both written once
    # at the very end of a run. Kept the moment they appear, and never
    # blanked by a later read, on the same reasoning as the findings above —
    # this is the result the scan was run for, and a poll landing after the
    # scanner has cleaned up must not take it off the page.
    report = parse_report(artifacts.get('report'))
    if report:
        record['report'] = report
    coverage = artifacts.get('coverage')
    if isinstance(coverage, dict):
        record['coverage'] = coverage

    # Only a status the backend actually reported may move the record. An
    # empty read is the normal state for the first seconds of every scan —
    # the run record does not exist until the scanner writes it — and must
    # mean "no news", not "unknown status". Treating it as unknown would fire
    # the `failed` default below and kill every scan the moment it started.
    reported = run.get('status')
    if record.get('status') == 'running' and reported:
        mapped = map_backend_status(reported)
        if mapped != 'running':
            # A scan its user stopped ends `stopped`, whatever the scanner
            # writes on its way out. It records the signal it was sent as
            # "interrupted", its word for a crash too, so a Stop press used to
            # read on the page as "the scanner process ended unexpectedly".
            # A scan that genuinely completed in that moment still says so.
            if record.get('stop_requested_at') and mapped != 'done':
                mapped = 'stopped'
            record['status'] = mapped
            record['ended_at'] = time.time()
    return added, changed


def ended_badly(record):
    """Did this scan end some way other than finishing or its user stopping
    it? Those are the ends that owe the user a reason."""
    status = (record or {}).get('status')
    if status in ('failed', 'interrupted'):
        return True
    return status == 'stopped' and not record.get('stop_requested_at')


def fallback_reason(record):
    """Why a scan ended badly, when the backend could not say.

    A spending limit is visible from the record alone, so it is named even
    for a backend that explains nothing. Otherwise the sentence admits the
    reason is unknown, rather than leaving a gap the user reads as "no
    reason" or filling it with a guess.
    """
    record = record or {}
    budget = record.get('budget_usd')
    spent = (record.get('usage') or {}).get('cost_usd') or 0
    if budget and spent >= budget:
        return f'The scan reached its ${budget:.2f} spending limit.'
    return ('The scanner did not say why. The details are kept in the '
            'workspace server log.')


def _did_work(record):
    """Did the scan get as far as checking anything before it ended?"""
    usage = record.get('usage') or {}
    return bool(record.get('findings') or usage.get('requests'))


def covered_everything(record):
    """Did the scanner say its run was a full account of the app?

    True / False / None, and the None matters: a record from before coverage
    was read, or a backend that reports none, is UNKNOWN. Unknown must never
    be rendered as a yes — "nothing found" over a scan whose reach nobody can
    vouch for is the all-clear this whole module exists to avoid.
    """
    coverage = (record or {}).get('coverage')
    if not isinstance(coverage, dict) or 'complete' not in coverage:
        return None
    return bool(coverage.get('complete'))


def coverage_caveats(record):
    """The scanner's own sentences about what it did not get to."""
    coverage = (record or {}).get('coverage')
    if not isinstance(coverage, dict):
        return []
    caveats = coverage.get('caveats')
    return [c for c in caveats if isinstance(c, str) and c.strip()] \
        if isinstance(caveats, list) else []


#: How close to the cap counts as "the cap is what ended this". The scanner
#: stops testing BEFORE the limit and keeps the rest to write its report, so a
#: scan that ends near the cap was ended by the cap without ever reaching it —
#: which is why this is a fraction and not an equality.
BUDGET_SPENT_FRACTION = 0.85


def budget_note(record):
    """One sentence when the spending limit is what cut a scan short, else ''.

    The meter already shows the number; this says what the number MEANT,
    which is the part the user cannot infer. A scan that stops at $2.76 of
    $3.00 and reports nothing looks like a clean result and is not one.
    """
    record = record or {}
    if not is_terminal(record.get('status')):
        return ''
    budget = record.get('budget_usd')
    spent = (record.get('usage') or {}).get('cost_usd') or 0
    if not budget or spent < budget * BUDGET_SPENT_FRACTION:
        return ''
    return (f'This scan used ${spent:.2f} of its ${budget:.2f} limit. The '
            f'scanner stops testing before the limit is reached, keeping the '
            f'rest to write its report — so a higher limit buys more testing '
            f'time.')


#: Why a terminal scan ended, as one code a page can pick its headline and
#: next step by — "Try again", "Model settings", "Scan again" — without
#: pattern-matching the reason's wording. Derived on the way out by
#: `end_reason_of`, never stored, so a record an older build wrote answers
#: with a code too.
END_REASONS = ('completed', 'user_stopped', 'budget_reached',
               'provider_error', 'scanner_error', 'process_vanished',
               'workspace_restarted', 'lost_track')

#: The backend's `cause` (see `ScanBackend.explain_end`) → our end reason.
#: `lost` is ours, written when the poller watching a scan crashed.
_CAUSE_REASONS = {
    'budget': 'budget_reached',
    'provider': 'provider_error',
    'scanner': 'scanner_error',
    'lost': 'lost_track',
}


def end_reason_of(record):
    """The `END_REASONS` code for a terminal scan, or '' while it runs.

    Who ended it comes first, because that is certain: a user's stop, the
    workspace restarting. Then what the backend said caused it. With neither,
    a spent budget is still visible from the record alone; a process that
    died mid-scan is `process_vanished`; anything else that went wrong is
    `scanner_error`, whose reason sentence admits it is unknown.
    """
    record = record or {}
    status = record.get('status')
    if not is_terminal(status):
        return ''
    if status == 'done':
        return 'completed'
    if status == 'stopped' and record.get('stop_requested_at'):
        return 'user_stopped'
    if record.get('interrupted_reason') == 'restart':
        return 'workspace_restarted'
    cause = _CAUSE_REASONS.get(record.get('end_cause') or '')
    if cause:
        return cause
    budget = record.get('budget_usd')
    if budget and ((record.get('usage') or {}).get('cost_usd') or 0) >= budget:
        return 'budget_reached'
    return 'process_vanished' if status == 'interrupted' else 'scanner_error'


# ── what leaves the server ─────────────────────────────────────────────────

#: Keys never included in a list response. `findings` because the list would
#: otherwise carry every reproduction for every scan on one page, `report`
#: for the same reason, and `handle` because it is the backend's private
#: bookkeeping. `coverage` stays: it is two fields, and without it a list row
#: cannot tell a clean scan from one that never got through the app.
_SUMMARY_DROP = ('findings', 'report', 'handle', 'dispositions')


def summarise(record):
    """One row for the scans list — no findings, no backend internals."""
    row = {k: v for k, v in (record or {}).items() if k not in _SUMMARY_DROP}
    row['end_reason'] = end_reason_of(record)
    return row


def public_view(record):
    """One scan in full: findings sorted, dispositions attached alongside.

    Dispositions ride beside the findings rather than inside them so the
    scanner's own record stays byte-identical to what it wrote.
    """
    record = record or {}
    findings = sort_findings(record.get('findings'))
    view = {k: v for k, v in record.items() if k not in ('handle',)}
    view['findings'] = findings
    view['dispositions'] = record.get('dispositions') or {}
    view['end_reason'] = end_reason_of(record)
    return view


def result_summary(record):
    """One honest sentence about how a finished scan ended.

    Never "0 vulnerabilities" on its own. Too many failure modes here produce
    an empty findings list — the target refused connections, credentials were
    rejected, the run died — and an unqualified all-clear over any of them is
    worse than no answer at all. So the sentence always says what was reached
    and how the scan ended, and a clean result says so only when the scan
    actually completed.

    The reason a scan ended badly is NOT folded in: it is the record's
    `error`, which every surface shows beside this sentence. Folding it in as
    well printed it twice on the page, once inside the sentence and once
    under it.
    """
    record = record or {}
    status = record.get('status')
    target = (record.get('target') or {})
    where = target.get('name') or f"port {target.get('port')}"
    counts = record.get('counts') or {}
    total = counts.get('total', 0)
    mode = record.get('mode') or DEFAULT_SCAN_MODE

    if status == 'running':
        return f'Scanning {where}…'
    if status == 'failed':
        # The scanner also fails a scan that crashed halfway, after real
        # work. "Nothing was checked" would undersell what is listed below.
        if _did_work(record):
            return (f'The scan of {where} failed partway through, so only '
                    f'part of the app was checked.')
        return f'The scan of {where} could not run, so nothing was checked.'
    if status == 'interrupted':
        # Two different paths reach `interrupted`. The boot sweep knows the
        # workspace restarted; the poller only knows the process vanished
        # under it (an OOM kill, a `kill` from a terminal, dind taking the
        # sandbox down). Claiming a restart for the second case states
        # something that did not happen, on the one surface whose whole
        # thesis is never misrepresenting how a scan ended -- so each path
        # records why, and an older record with no reason gets the sentence
        # that is true either way.
        if record.get('interrupted_reason') == 'restart':
            return (f'The scan of {where} stopped when the workspace '
                    f'restarted. Anything found before that is below; the '
                    f'rest was not checked.')
        # With a reason on the record, "unexpectedly" would contradict the
        # sentence printed right beside it.
        how = '' if record.get('error') else (
            ', because the scanner process ended unexpectedly')
        return (f'The scan of {where} stopped before it finished{how}. '
                f'Anything found before that is below; the rest was not '
                f'checked.')
    if status == 'stopped':
        if record.get('stop_requested_at'):
            return (f'You stopped the scan of {where} before it finished, so '
                    f'only part of the app was checked.')
        if record.get('error'):
            # Ended by the scanner itself — its spending limit, a provider
            # refusing requests. The error says which.
            return (f'The scanner ended the scan of {where} early, so only '
                    f'part of the app was checked.')
        # A record from before stops were marked: it cannot say who.
        return (f'The scan of {where} was stopped early, so only part of the '
                f'app was checked.')
    # A scan can reach `done` having proven nothing and still not be an
    # all-clear: the scanner writes its report, marks the run complete, and
    # separately records that it never got through the app. Saying "finished
    # and found nothing" over that is the exact sentence this user read as
    # "my app is clean".
    complete = covered_everything(record)
    if total:
        if complete is False:
            return (f'A {mode} scan of {where} found {total} to look at, and '
                    f'did not get through the whole app — there may be more.')
        return f'A {mode} scan of {where} found {total} to look at.'
    if complete is False:
        return (f'A {mode} scan of {where} finished without proving anything, '
                f'but the scanner says it did not get through the whole app. '
                f'This is not an all-clear.')
    return (f'A {mode} scan of {where} finished and found nothing. A deeper '
            f'scan checks more than a {mode} one does.')


#: Anything shaped like a credential is removed before scanner output is shown
#: in the UI or written to a scan record. The scanner does not print its key,
#: but a provider's error message can echo one back, and that text is exactly
#: what we surface when a scan fails to start.
_SECRET_RE = re.compile(
    r'(sk-[A-Za-z0-9_\-]{8,}'
    r'|(?:api[_-]?key|token|authorization|bearer)\s*[:=]\s*\S+)',
    re.IGNORECASE)


def redact_secrets(text):
    """Blank out credential-shaped substrings in scanner output."""
    return _SECRET_RE.sub('[redacted]', text or '')


# ── the manager (impure: filesystem, threads, events) ──────────────────────

class ScansManager:
    """Owns scan records, one poller thread per running scan, and the sweep.

    The impure half of this module, following the same split `boards/runs.py`
    uses: the functions above are pure and carry the decisions, this class
    carries the state. It reaches the event bus and the feed through callables
    injected by `set_publishers`, never by importing server.py — a handler-side
    module that imports server executes a second copy of it under a second
    name, with duplicate managers and duplicate background threads.

    **Leases and TTLs are deliberately absent.** A scan is owned by exactly one
    process, so there is nothing to arbitrate. What a restart needs is the
    boot sweep: at startup no worker of a previous process can still be alive,
    so a record still claiming `running` is definitively orphaned. That is why
    `interrupted` exists as a status rather than leaving a dead scan sitting at
    `running` forever, which is issue #462's failure mode.
    """

    #: The scanner rewrites its record on every finding, so this is how quickly
    #: a new finding reaches the UI. Two stats per tick — cheap enough to be
    #: frequent, slow enough not to matter.
    POLL_INTERVAL = 3.0
    #: Scan folders hold findings and a console log; keep the recent ones.
    MAX_SCANS_KEPT = 100

    _lock = threading.Lock()
    _pollers = {}          # scan_id -> Thread
    _backend_factory = None
    _publish = None
    _emit_feed = None
    _targets_provider = None

    # ── wiring ─────────────────────────────────────────────────────────────

    @classmethod
    def configure(cls, *, backend_factory, publish=None, emit_feed=None,
                  targets_provider=None):
        """Inject collaborators. Called once from server.py at startup."""
        cls._backend_factory = backend_factory
        cls._publish = publish
        cls._emit_feed = emit_feed
        cls._targets_provider = targets_provider

    @classmethod
    def backend(cls):
        if cls._backend_factory is None:
            raise RuntimeError('scans backend is not configured')
        return cls._backend_factory()

    @classmethod
    def targets(cls):
        return list(cls._targets_provider() if cls._targets_provider else [])

    @classmethod
    def _fire(cls, event_type, data):
        """Publish, never raise. A telemetry failure must not end a scan."""
        if cls._publish is None:
            return
        try:
            cls._publish(event_type, data)
        except Exception:      # pragma: no cover - defensive
            pass

    # ── storage ────────────────────────────────────────────────────────────

    @classmethod
    def scans_dir(cls):
        return os.environ.get('KC_SCANS_DIR') or '/home/dev/.claude-scans'

    @classmethod
    def scan_dir(cls, scan_id):
        return os.path.join(cls.scans_dir(), scan_id)

    @classmethod
    def _record(cls, scan_id):
        return boards.store.JsonRecord(
            os.path.join(cls.scan_dir(scan_id), 'scan.json'))

    @classmethod
    def get(cls, scan_id):
        if not valid_scan_id(scan_id):
            return None
        rec = cls._record(scan_id).read()
        return rec or None

    @classmethod
    def list_scans(cls):
        """Newest first. Reads each record — there are at most MAX_SCANS_KEPT
        of them and they are small."""
        try:
            names = os.listdir(cls.scans_dir())
        except OSError:
            return []
        rows = []
        for name in names:
            if not valid_scan_id(name):
                continue
            rec = cls._record(name).read()
            if rec:
                rows.append(summarise(rec))
        rows.sort(key=lambda r: r.get('started_at') or 0, reverse=True)
        return rows

    # ── creating ───────────────────────────────────────────────────────────

    @classmethod
    def create(cls, spec):
        """Start a scan. Returns `(record, error)`; exactly one is None.

        Everything that can fail does so here, before the caller's HTTP
        response — resolving the address, checking the backend, spawning. What
        happens after this returns is the scan itself, which nobody waits for.
        """
        backend = cls.backend()
        ready = backend.preflight()
        if not ready.get('ok'):
            detail = ready.get('detail') or 'Scanning is not available.'
            cls._log(f'start refused for port {spec["port"]} '
                     f'({ready.get("reason") or "not ready"}): {detail}')
            return None, detail

        try:
            target_url = backend.resolve_target(spec['port'])
        except Exception as e:
            cls._log(f'start refused for port {spec["port"]}: {e}')
            return None, str(e)

        # Only now, once nothing before the spawn can fail without a record:
        # a directory with no record in it is invisible to `_prune`, so every
        # refused start used to leave one behind for good.
        scan_id = new_scan_id()
        directory = cls.scan_dir(scan_id)
        os.makedirs(directory, mode=0o700, exist_ok=True)

        record = {
            'id': scan_id,
            'status': 'running',
            'backend': backend.name,
            'target': {'port': spec['port'], 'name': spec.get('name') or '',
                       'url': target_url},
            'mode': spec['mode'],
            'model': spec['model'],
            'budget_usd': spec.get('budget_usd'),
            'instruction': spec.get('instruction') or '',
            'usage': parse_usage(None),
            'counts': count_severities([]),
            'findings': [],
            'dispositions': {},
            'started_at': time.time(),
            'ended_at': None,
            'error': None,
            'handle': None,
        }

        try:
            record['handle'] = backend.start(
                dict(spec, target_url=target_url, scan_dir=directory))
        except Exception as e:
            record['status'] = 'failed'
            record['ended_at'] = time.time()
            record['error'] = redact_secrets(str(e))
            cls._record(scan_id).write(record)
            cls._log(f'{scan_id} could not start: {record["error"]}')
            return None, record['error']

        cls._record(scan_id).write(record)
        cls._log(f'{scan_id} started on port {spec["port"]} '
                 f'({spec.get("name") or "unnamed"}): {spec["mode"]} scan, '
                 f'model {spec["model"]}, budget '
                 f'{spec.get("budget_usd") or "none"}')
        cls._prune()
        cls._fire('scan.status', {'id': scan_id, 'status': 'running'})
        cls._spawn_poller(scan_id)
        return record, None

    # ── the poller ─────────────────────────────────────────────────────────

    @classmethod
    def _spawn_poller(cls, scan_id):
        with cls._lock:
            existing = cls._pollers.get(scan_id)
            if existing and existing.is_alive():
                return
            thread = threading.Thread(target=cls._poll_loop, args=(scan_id,),
                                      name=f'scan-{scan_id}', daemon=True)
            cls._pollers[scan_id] = thread
        thread.start()

    @classmethod
    def _poll_loop(cls, scan_id):
        backend = cls.backend()
        rec = cls._record(scan_id)
        try:
            while True:
                current = rec.read()
                if not current or current.get('status') != 'running':
                    return
                handle = current.get('handle')
                artifacts = cls._safe_artifacts(backend, handle)
                alive = backend.is_alive(handle)
                cls._absorb(scan_id, artifacts, alive=alive, backend=backend,
                            handle=handle)
                if cls._record(scan_id).read().get('status') != 'running':
                    return
                time.sleep(cls.POLL_INTERVAL)
        except Exception:
            cls._log(f'{scan_id} poller crashed:\n{traceback.format_exc()}')
            cls._settle_lost(scan_id, backend)
        finally:
            with cls._lock:
                cls._pollers.pop(scan_id, None)

    #: What the page says when the poller died under a scan.
    LOST_TRACK_REASON = ('kube-coder lost track of this scan because of an '
                         'internal error, so it stopped it rather than let it '
                         'run unwatched. Scan again; if this repeats, the '
                         'details are in the workspace server log.')

    @classmethod
    def _settle_lost(cls, scan_id, backend):
        """End a scan whose poller crashed, instead of leaving it on
        "Scanning…" forever while nothing reads its results.

        Stopped rather than resumed: whatever crashed the poller would most
        likely crash a new one too, and a scan nobody watches still spends.
        """
        try:
            record = cls.get(scan_id) or {}
            if record.get('status') != 'running':
                return
            try:
                backend.stop(record.get('handle'))
            except Exception:
                cls._log(f'{scan_id} stop after poller crash failed:\n'
                         f'{traceback.format_exc()}')

            def mutate(rec):
                if rec.get('status') != 'running':
                    return False
                rec['status'] = 'failed'
                rec['ended_at'] = time.time()
                rec['end_cause'] = 'lost'
                rec['error'] = cls.LOST_TRACK_REASON
                return None

            settled, wrote = cls._record(scan_id).update(mutate)
            if wrote:
                cls._fire('scan.status', {'id': scan_id, 'status': 'failed'})
                if not settled.get('quiet'):
                    cls._announce(settled)
        except Exception:
            # Last resort: the boot sweep settles it on the next restart.
            cls._log(f'{scan_id} could not be settled after its poller '
                     f'crashed:\n{traceback.format_exc()}')

    @classmethod
    def _safe_artifacts(cls, backend, handle):
        """A backend read that cannot end a scan by raising. `None` is the
        backend's own "could not read this time", so a failure costs one
        poll of freshness and nothing else."""
        try:
            return backend.artifacts(handle)
        except Exception:
            cls._log(f'artifacts read failed for {handle}:\n'
                     f'{traceback.format_exc()}')
            return None

    @classmethod
    def _absorb(cls, scan_id, artifacts, *, alive, backend, handle):
        """Fold one read into the record, then emit exactly what changed."""
        added, changed = [], []
        evidence = ''

        def mutate(record):
            nonlocal added, changed, evidence
            if record.get('status') != 'running':
                return False
            added, changed = apply_artifacts(record, artifacts)
            if record['status'] == 'running' and not alive:
                # The process is gone but its record never reached a terminal
                # state. If its user asked it to stop, that is what happened.
                # Otherwise it died rather than finished: `failed` when it
                # never produced anything (a bad model or credential exits at
                # once), `interrupted` when it was working and stopped
                # mid-flight.
                if record.get('stop_requested_at'):
                    record['status'] = 'stopped'
                elif not artifacts:
                    record['status'] = 'failed'
                else:
                    record['status'] = 'interrupted'
                    record['interrupted_reason'] = 'vanished'
                record['ended_at'] = time.time()
            # In the same write as the status, so the page never shows a
            # failed scan without its reason, even for one poll.
            if ended_badly(record) and not record.get('error'):
                record['error'], evidence, cause = cls._explain(
                    record, backend, handle)
                if cause:
                    record['end_cause'] = cause
            return None

        record, wrote = cls._record(scan_id).update(mutate)
        if not wrote:
            return record
        if evidence:
            cls._log_end(record, evidence)

        for finding in added:
            cls._fire('scan.finding', {'id': scan_id, 'op': 'added',
                                       'finding_id': finding.get('id'),
                                       'severity': severity_of(finding)})
        for finding in changed:
            cls._fire('scan.finding', {'id': scan_id, 'op': 'changed',
                                       'finding_id': finding.get('id'),
                                       'severity': severity_of(finding)})
        cls._fire('scan.usage', {'id': scan_id, 'usage': record.get('usage'),
                                 'counts': record.get('counts')})
        if is_terminal(record.get('status')):
            location = (record.get('report') or {}).get('location')
            if location:
                cls._log(f'{scan_id} report saved at {location}')
            cls._fire('scan.status', {'id': scan_id,
                                      'status': record['status'],
                                      'counts': record.get('counts')})
            # A scan stopped on the way to being deleted is not announced:
            # the feed item's "Open scan" link would 404 a moment later.
            if not record.get('quiet'):
                cls._announce(record)
        return record

    @staticmethod
    def _explain(record, backend, handle):
        """`(reason for the user, evidence for the server log, cause)`.

        The backend is the only thing that knows its scanner's output, exit
        status and stated reasons, so it is asked. When it cannot say, the
        reason says the reason is unknown — never an empty gap, which is what
        a scan that "failed with no reason" looked like. Both text halves are
        redacted: a provider's error can quote the key back.
        """
        try:
            reason, evidence, cause = backend.explain_end(handle)
        except Exception:           # a reason is never worth a lost status
            reason, cause = '', ''
            evidence = f'explain_end raised:\n{traceback.format_exc()}'
        reason = redact_secrets(reason).strip() or fallback_reason(record)
        return (reason, redact_secrets(evidence) or 'no further detail',
                cause if cause in _CAUSE_REASONS else '')

    @classmethod
    def _log(cls, message):
        print(f'[scan] {message}', file=sys.stderr, flush=True)

    @classmethod
    def _log_end(cls, record, evidence):
        """One server-log entry for a scan that ended badly, with everything
        needed to debug it later — the UI carries only the plain reason."""
        usage = record.get('usage') or {}
        took = (record.get('ended_at') or time.time()) - (
            record.get('started_at') or time.time())
        target = record.get('target') or {}
        lines = [
            f'{record.get("id")} ended {record.get("status")} after '
            f'{int(took)}s on port {target.get("port")}, model '
            f'{record.get("model")}, spent ${usage.get("cost_usd") or 0:.4f} '
            f'over {usage.get("requests") or 0} request(s)',
            f'reason shown: {record.get("error")}',
        ]
        lines.extend(evidence.splitlines())
        cls._log(f'\n[scan]   '.join(lines))

    @classmethod
    def _announce(cls, record):
        """One feed item per finished scan.

        `waiting` is what turns a feed item into a phone notification, so it is
        set only when there is something to act on. A clean scan is recorded
        without buzzing anybody — a security tool that pings on every all-clear
        gets muted, and then it pings about nothing when it matters.
        """
        if cls._emit_feed is None:
            return
        counts = record.get('counts') or {}
        total = counts.get('total', 0)
        worst = highest_severity(record.get('findings'))
        needs_attention = bool(total) or record.get('status') == 'failed'
        target = (record.get('target') or {})
        where = target.get('name') or f"port {target.get('port')}"
        if total:
            title = f'Security scan of {where}: {total} to look at'
        elif record.get('status') == 'failed':
            title = f'Security scan of {where} could not finish'
        else:
            title = f'Security scan of {where} finished'
        body = result_summary(record)
        if record.get('error'):
            body += f'\n\n{record["error"]}'
        # The report outlives the page and the notification, so say where it
        # is kept: the path is the backend's own label, passed through as is.
        location = (record.get('report') or {}).get('location')
        if location:
            body += f"\n\nThe scanner's report is saved at `{location}`."
        try:
            cls._emit_feed(
                'activity', title,
                body_md=body,
                source='scan',
                links=[{'label': 'Open scan', 'ref': f'scan:{record["id"]}'}],
                waiting=needs_attention,
                dedupe_key=f'scan:{record["id"]}',
            )
        except Exception as e:       # pragma: no cover - defensive
            print(f'[scan] feed emit failed: {e}', file=sys.stderr)
        cls._fire('scan.done', {'id': record['id'], 'total': total,
                                'highest': worst})

    # ── stopping, dispositions, housekeeping ───────────────────────────────

    @classmethod
    def stop(cls, scan_id, *, announce=True):
        """End a running scan. Returns the record, or None when unknown.

        `announce=False` is for the delete path: announcing posts a feed item
        and a push whose "Open scan" link points at a scan that is removed on
        the next line, so the row outlives its target and the link 404s.
        """
        record = cls.get(scan_id)
        if record is None:
            return None
        if is_terminal(record.get('status')):
            return record

        # Marked BEFORE the scanner is signalled. It takes up to its grace
        # period to exit, and the poller reads the record every few seconds
        # meanwhile; without the mark, the poller found the process gone (or
        # the scanner's own "interrupted", which it writes for any signal)
        # and settled the scan as a crash before this call could say it was
        # the user's.
        def request(rec):
            if rec.get('status') != 'running':
                return False
            rec['stop_requested_at'] = time.time()
            if not announce:
                rec['quiet'] = True
            return None

        record, wrote = cls._record(scan_id).update(request)
        if not wrote:
            return record or None    # it ended on its own meanwhile
        cls._log(f'{scan_id} stop requested by the user')

        backend = cls.backend()
        handle = record.get('handle')
        try:
            backend.stop(handle)
        except Exception:
            cls._log(f'{scan_id} stop failed:\n{traceback.format_exc()}')
        if backend.is_alive(handle):
            # Still settled below — the user's stop is not undone — but a
            # scanner that outlived its kill keeps spending, so it is said
            # loudly where whoever runs this workspace will look.
            cls._log(f'{scan_id} is STILL RUNNING after its stop; it may '
                     f'keep spending until it exits on its own')
        # One last read, because the scanner saves its spend and findings on
        # the way out. Settled as ended even if the signal failed: a scan the
        # user stopped must not sit at `running` forever.
        artifacts = cls._safe_artifacts(backend, handle)
        record = cls._absorb(scan_id, artifacts, alive=False, backend=backend,
                             handle=handle)
        cls._log(f'{scan_id} stop complete: {(record or {}).get("status")} '
                 f'({end_reason_of(record)})')
        return record

    @classmethod
    def set_disposition(cls, scan_id, finding_id, disposition):
        """Mark a finding open or dismissed. Stored beside the findings, never
        inside them — the scanner's own record is not ours to edit."""
        if disposition not in ('open', 'dismissed'):
            return None, 'Unknown disposition.'
        found = [False]

        def mutate(rec):
            if not any(f.get('id') == finding_id
                       for f in rec.get('findings') or []):
                return False
            found[0] = True
            dispositions = rec.setdefault('dispositions', {})
            if disposition == 'open':
                dispositions.pop(finding_id, None)
            else:
                dispositions[finding_id] = disposition
            return None

        record, wrote = cls._record(scan_id).update(mutate)
        if not found[0]:
            return None, 'No such finding in this scan.'
        return record, None

    @classmethod
    def delete(cls, scan_id):
        """Remove a scan and everything it produced."""
        record = cls.get(scan_id)
        if record is None:
            return False
        if not is_terminal(record.get('status')):
            cls.stop(scan_id, announce=False)
        shutil.rmtree(cls.scan_dir(scan_id), ignore_errors=True)
        cls._fire('scan.status', {'id': scan_id, 'status': 'deleted'})
        return True

    @classmethod
    def _prune(cls):
        """Keep the newest MAX_SCANS_KEPT finished scans; drop the rest.

        Findings and console logs accumulate on a PVC that nothing else
        reclaims, and an unbounded store is a slow disk-full. Running scans are
        never pruned regardless of age.
        """
        rows = [r for r in cls.list_scans() if is_terminal(r.get('status'))]
        for row in rows[cls.MAX_SCANS_KEPT:]:
            shutil.rmtree(cls.scan_dir(row['id']), ignore_errors=True)

    @classmethod
    def start(cls):
        """Boot sweep, then resume anything genuinely still running.

        At startup no process of a previous run can still be alive, so a record
        that says `running` is stale unless its process survived — which only
        happens when server.py restarted under a scan it had spawned. Both
        cases are handled by asking the backend, rather than by guessing from
        a timestamp.
        """
        touched = []
        backend = cls.backend()
        try:
            names = sorted(os.listdir(cls.scans_dir()))
        except OSError:
            return touched
        for name in names:
            if not valid_scan_id(name):
                continue
            record = cls._record(name).read()
            if not record or record.get('status') != 'running':
                continue
            handle = record.get('handle')
            if backend.is_alive(handle):
                cls._spawn_poller(name)
                continue

            def mutate(rec):
                if rec.get('status') != 'running':
                    return False
                rec['ended_at'] = time.time()
                if rec.get('stop_requested_at'):
                    # The restart landed mid-stop: the user's request stands.
                    rec['status'] = 'stopped'
                    return None
                rec['status'] = 'interrupted'
                rec['interrupted_reason'] = 'restart'
                rec['error'] = ('The workspace restarted while this scan was '
                                'running, so it did not finish.')
                return None

            settled, wrote = cls._record(name).update(mutate)
            if wrote:
                touched.append(name)
                cls._log(f'{name} settled {settled["status"]} at boot: its '
                         f'process did not survive the server restart')
                cls._fire('scan.status', {'id': name,
                                          'status': settled['status']})
        return touched
