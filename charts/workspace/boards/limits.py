"""Three-tier rate limiting for board writes.

A single `requests_per_minute` is the wrong shape, because the three vendors we
target throttle on three genuinely different axes:

- **Zendesk** — account-wide by plan (200/400/700/2500 rpm) with Retry-After
  and ratelimit-reset, BUT Update Ticket is 30 per 10 minutes *per user per
  ticket* and Incremental Exports is 10/min. The per-ticket cap is the binding
  one for a board processor: comment + status + assign on one ticket burns 3.
- **Asana** — allocated per authorization token.
- **GitHub** — secondary rate limits return status **200 or 403, not 429**, and
  there are separate point budgets (900/min REST, 2000/min GraphQL) alongside
  the hourly primary limit.

So: a global limiter, per-action overrides, a distinct per-item write budget,
Retry-After/ratelimit-reset honouring, and limit DETECTION that is configurable
per adapter rather than a hardcoded 429 check.

Backoff is deliberately GLOBAL rather than per-worker. The budget is account-
wide, so one worker discovering a limit must pause the whole run — otherwise
the other workers keep spending the budget that the pause was meant to protect.
"""

import collections
import threading
import time


class RateLimiter:
    """Sliding-window per-key limiter (N events / window seconds). In-memory.

    Same algorithm and interface as gateway.RateLimiter (gateway.py:681);
    duplicated rather than imported so the boards package stays importable on
    its own with no dependency on the messaging gateway. If a third copy ever
    appears, that is the signal to promote it to a shared module.
    """

    def __init__(self, max_events=20, window_seconds=60.0):
        self.max = max_events
        self.window = float(window_seconds)
        self._events = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def allow(self, key):
        now = time.time()
        with self._lock:
            dq = self._events[key]
            while dq and now - dq[0] > self.window:
                dq.popleft()
            if len(dq) >= self.max:
                return False
            dq.append(now)
            return True

    def retry_after(self, key):
        """Seconds until `key` would be allowed again (0 when it already is)."""
        now = time.time()
        with self._lock:
            dq = self._events[key]
            while dq and now - dq[0] > self.window:
                dq.popleft()
            if len(dq) < self.max:
                return 0.0
            return max(0.0, self.window - (now - dq[0]))


class LimitExceeded(Exception):
    """A local budget refused the write before it was sent. Carries the tier
    that refused so the caller can tell "this board is busy" (retryable) from
    "this ticket has had enough writes" (needs a human)."""

    def __init__(self, tier, detail, retry_after=0.0):
        super().__init__(detail)
        self.tier = tier
        self.detail = detail
        self.retry_after = retry_after


DEFAULT_GLOBAL = {'max_events': 300, 'window_seconds': 60}
# Absent an explicit config we assume the vendor signals limits the ordinary
# way. 403 is NOT in the default set: a plain 403 is usually a permission
# problem, and treating it as a rate limit would retry a request that can never
# succeed. GitHub's connector opts into 403 explicitly, paired with a body
# phrase so an ordinary permission denial is still reported as one.
DEFAULT_DETECT = {'statuses': [429], 'body_contains': []}


# Zendesk's Update Ticket cap — 30 per 10 minutes per user per ticket — is the
# shape this window is sized for. It must be a WINDOW rather than a lifetime
# total: a permanent "this ticket has had 30 writes, ever" cap would silently
# retire a busy ticket forever.
DEFAULT_PER_ITEM_WINDOW = 600.0


class BoardLimiter:
    """Enforces one connector's declared limits across a whole run.

    The per-item write budget can be made DURABLE by handing in a `write_log`
    (previously recorded writes) and an `on_write` callback that persists new
    ones. Without them the budget is per-process, which is fine for a single
    action but wrong for a run: a pod restart mid-run would reset every
    ticket's budget to zero and let the run spend it twice.
    """

    def __init__(self, limits=None, *, write_log=None, on_write=None):
        limits = limits or {}
        g = limits.get('global') or DEFAULT_GLOBAL
        self._global = RateLimiter(g.get('max_events', 300),
                                   g.get('window_seconds', 60))
        self._per_action = {
            name: RateLimiter(cfg.get('max_events', 30),
                              cfg.get('window_seconds', 600))
            for name, cfg in (limits.get('per_action') or {}).items()
        }
        self._per_item_writes = limits.get('per_item_writes')
        self._per_item_window = float(
            limits.get('per_item_writes_window_seconds')
            or DEFAULT_PER_ITEM_WINDOW)
        self._detect = limits.get('limit_detect') or DEFAULT_DETECT
        self._retry_after_headers = [
            h.lower() for h in (limits.get('retry_after_headers')
                                or ['retry-after', 'ratelimit-reset'])
        ]
        # key -> [[timestamp, cost], ...]. A log rather than a counter so the
        # budget can be a sliding window and so a persisted log can be replayed
        # after a restart without knowing when the window started.
        self._writes = collections.defaultdict(list)
        for key, entries in (write_log or {}).items():
            self._writes[key] = [[float(ts), int(cost)] for ts, cost in entries]
        self._on_write = on_write
        self._paused_until = 0.0
        self._lock = threading.Lock()

    def _spent(self, key, now):
        """Cost recorded for `key` inside the window, pruning what fell out.
        Caller holds the lock."""
        entries = self._writes[key]
        cutoff = now - self._per_item_window
        if entries and entries[0][0] <= cutoff:
            entries[:] = [e for e in entries if e[0] > cutoff]
        return sum(cost for _ts, cost in entries)

    # ── budgets checked BEFORE a request goes out ──────────────────────────

    def check(self, board_id, item_id, action_name, cost=1):
        """Raise LimitExceeded if this write is not currently permitted.

        Order matters: the per-item budget is checked first because it is the
        one whose exhaustion is NOT transient — no amount of waiting frees it
        within the run, so failing fast there avoids burning a global slot on a
        write that is going to be refused anyway."""
        now = time.time()
        with self._lock:
            paused_for = self._paused_until - now
            if paused_for > 0:
                raise LimitExceeded(
                    'global-backoff',
                    f'run is backing off for {paused_for:.0f}s after the board '
                    f'reported a rate limit', paused_for)

            if self._per_item_writes is not None:
                key = f'{board_id}:{item_id}'
                spent = self._spent(key, now)
                if spent + cost > self._per_item_writes:
                    raise LimitExceeded(
                        'per-item',
                        f'item {item_id} has used its write budget '
                        f'({spent}/{self._per_item_writes} in the last '
                        f'{self._per_item_window:.0f}s); '
                        f'{action_name!r} needs {cost} more')

        limiter = self._per_action.get(action_name)
        if limiter is not None and not limiter.allow(f'{board_id}:{action_name}'):
            raise LimitExceeded(
                'per-action',
                f'action {action_name!r} is rate limited for this board',
                limiter.retry_after(f'{board_id}:{action_name}'))

        if not self._global.allow(board_id):
            raise LimitExceeded(
                'global', f'board {board_id} is rate limited',
                self._global.retry_after(board_id))

        key = f'{board_id}:{item_id}'
        stamp = time.time()
        with self._lock:
            self._writes[key].append([stamp, cost])
        # Persist OUTSIDE the lock: on_write goes to disk under its own file
        # lock, and holding two locks in an order this class cannot see is how
        # deadlocks get introduced. The budget is already reserved in memory,
        # so a concurrent check cannot overspend while this is in flight.
        if self._on_write is not None:
            self._on_write(key, stamp, cost)

    # ── reacting to what the board actually said ───────────────────────────

    def is_limit_response(self, status, body):
        """Whether a response means "you are being throttled".

        Configurable per adapter rather than a hardcoded 429 check, because
        GitHub's secondary limits arrive as 200 or 403. When a status is listed
        AND body phrases are configured, the phrase must also match — that is
        what keeps an ordinary 403 (permission denied) from being mistaken for
        a limit and retried forever."""
        statuses = self._detect.get('statuses') or []
        phrases = self._detect.get('body_contains') or []
        if status not in statuses:
            return False
        if not phrases:
            return True
        try:
            text = (body or b'').decode('utf-8', 'replace').lower()
        except Exception:
            return False
        return any(p.lower() in text for p in phrases)

    def note_limit_response(self, headers, default_backoff=30.0):
        """Pause the WHOLE run, honouring the board's own Retry-After hint.

        Returns the number of seconds paused. Header values may be either a
        delta-seconds count (Retry-After) or an absolute epoch (Zendesk's
        ratelimit-reset), so both are accepted."""
        wait = default_backoff
        lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
        for name in self._retry_after_headers:
            raw = lowered.get(name)
            if raw is None:
                continue
            try:
                value = float(str(raw).strip())
            except (TypeError, ValueError):
                continue
            now = time.time()
            # An absolute epoch is far larger than any sane delta.
            wait = max(0.0, value - now) if value > now - 86400 and value > 10_000 \
                else max(0.0, value)
            break
        wait = max(0.0, min(wait, 900.0))   # never park a run for >15 min
        with self._lock:
            self._paused_until = max(self._paused_until, time.time() + wait)
        return wait

    # ── introspection ──────────────────────────────────────────────────────

    def writes_used(self, board_id, item_id):
        """Cost spent on this item inside the current window."""
        with self._lock:
            return self._spent(f'{board_id}:{item_id}', time.time())

    def write_log_snapshot(self):
        """The in-window write log, pruned — what a caller persists. Keys are
        `<board_id>:<item_id>`."""
        now = time.time()
        with self._lock:
            out = {}
            for key in list(self._writes):
                self._spent(key, now)          # prunes in place
                if self._writes[key]:
                    out[key] = [list(e) for e in self._writes[key]]
            return out

    def paused_for(self):
        with self._lock:
            return max(0.0, self._paused_until - time.time())
