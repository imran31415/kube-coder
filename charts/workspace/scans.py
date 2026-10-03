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
    never be pointed at a port nothing is serving.
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

    # Only a status the backend actually reported may move the record. An
    # empty read is the normal state for the first seconds of every scan —
    # the run record does not exist until the scanner writes it — and must
    # mean "no news", not "unknown status". Treating it as unknown would fire
    # the `failed` default below and kill every scan the moment it started.
    reported = run.get('status')
    if record.get('status') == 'running' and reported:
        mapped = map_backend_status(reported)
        if mapped != 'running':
            record['status'] = mapped
            record['ended_at'] = time.time()
    return added, changed


# ── what leaves the server ─────────────────────────────────────────────────

#: Keys never included in a list response. `findings` because the list would
#: otherwise carry every reproduction for every scan on one page, and `handle`
#: because it is the backend's private bookkeeping.
_SUMMARY_DROP = ('findings', 'handle', 'dispositions')


def summarise(record):
    """One row for the scans list — no findings, no backend internals."""
    return {k: v for k, v in (record or {}).items() if k not in _SUMMARY_DROP}


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
    return view


def result_summary(record):
    """One honest sentence about how a finished scan ended.

    Never "0 vulnerabilities" on its own. Too many failure modes here produce
    an empty findings list — the target refused connections, credentials were
    rejected, the run died — and an unqualified all-clear over any of them is
    worse than no answer at all. So the sentence always says what was reached
    and how the scan ended, and a clean result says so only when the scan
    actually completed.
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
        return (f'The scan of {where} could not run, so nothing was checked. '
                f'{record.get("error") or ""}'.strip())
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
        return (f'The scan of {where} stopped before it finished, because '
                f'the scanner process ended unexpectedly. Anything found '
                f'before that is below; the rest was not checked.')
    if status == 'stopped':
        return (f'The scan of {where} was stopped early, so only part of the '
                f'app was checked.')
    if total:
        return f'A {mode} scan of {where} found {total} to look at.'
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
            return None, ready.get('detail') or 'Scanning is not available.'

        scan_id = new_scan_id()
        directory = cls.scan_dir(scan_id)
        os.makedirs(directory, mode=0o700, exist_ok=True)

        try:
            target_url = backend.resolve_target(spec['port'])
        except Exception as e:
            return None, str(e)

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
            return None, record['error']

        cls._record(scan_id).write(record)
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
        except Exception as e:       # pragma: no cover - defensive
            print(f'[scan] {scan_id} poller stopped: {e}', file=sys.stderr)
        finally:
            with cls._lock:
                cls._pollers.pop(scan_id, None)

    @staticmethod
    def _safe_artifacts(backend, handle):
        """A backend read that cannot end a scan by raising."""
        try:
            return backend.artifacts(handle)
        except Exception:
            return None

    @classmethod
    def _absorb(cls, scan_id, artifacts, *, alive, backend, handle):
        """Fold one read into the record, then emit exactly what changed."""
        added, changed = [], []

        def mutate(record):
            nonlocal added, changed
            if record.get('status') != 'running':
                return False
            added, changed = apply_artifacts(record, artifacts)
            if record['status'] == 'running' and not alive:
                # The process is gone but its record never reached a terminal
                # state: it died rather than finished. `failed` when it never
                # produced anything (a bad model or credential exits at once),
                # `interrupted` when it was working and stopped mid-flight.
                if not artifacts:
                    record['status'] = 'failed'
                    record['error'] = cls._failure_detail(backend, handle)
                else:
                    record['status'] = 'interrupted'
                    record['interrupted_reason'] = 'vanished'
                record['ended_at'] = time.time()
            return None

        record, wrote = cls._record(scan_id).update(mutate)
        if not wrote:
            return record

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
            cls._fire('scan.status', {'id': scan_id,
                                      'status': record['status'],
                                      'counts': record.get('counts')})
            cls._announce(record)
        return record

    @staticmethod
    def _failure_detail(backend, handle):
        """Why a scan died before producing anything, in the user's words.

        The scanner's own console output is the only thing that knows, and it
        is written for a person — a missing credential, an unreachable model.
        Redacted, because a provider's error can quote the key back.
        """
        tail = ''
        try:
            tail = backend.tail_log(handle)
        except Exception:
            pass
        tail = redact_secrets(tail).strip()
        if not tail:
            return ('The scan stopped before it checked anything, and left no '
                    'explanation.')
        lines = [ln for ln in tail.splitlines() if ln.strip()][-6:]
        return '\n'.join(lines)

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
        title = (f'Security scan of {where}: {total} to look at'
                 if total else f'Security scan of {where} finished')
        try:
            cls._emit_feed(
                'activity', title,
                body_md=result_summary(record),
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
        try:
            cls.backend().stop(record.get('handle'))
        except Exception as e:
            print(f'[scan] {scan_id} stop failed: {e}', file=sys.stderr)

        def mutate(rec):
            if rec.get('status') != 'running':
                return False
            rec['status'] = 'stopped'
            rec['ended_at'] = time.time()
            return None

        record, wrote = cls._record(scan_id).update(mutate)
        if wrote:
            cls._fire('scan.status', {'id': scan_id, 'status': 'stopped'})
            if announce:
                cls._announce(record)
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
                rec['status'] = 'interrupted'
                rec['interrupted_reason'] = 'restart'
                rec['ended_at'] = time.time()
                rec['error'] = ('The workspace restarted while this scan was '
                                'running, so it did not finish.')
                return None

            _, wrote = cls._record(name).update(mutate)
            if wrote:
                touched.append(name)
                cls._fire('scan.status', {'id': name, 'status': 'interrupted'})
        return touched
