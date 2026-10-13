"""Is this workspace idle enough to be put to sleep? (#728)

The always-on keeper proposed in #728 scales `deploy/ws-<user>` to 0 when
nobody is using the workspace, and it needs the workspace itself to answer
that question — the keeper sits outside the pod and can see neither tmux nor
`~/.claude-tasks`. This module is that answer, and `GET /api/keeper/idle`
(handlers/system.py) is its wire format.

It is deliberately the *whole* decision input and none of the policy: the
keeper owns the threshold (`keeper.sleep.idleMinutes`) and owns the scaling.
Everything here is read-only observation.

FAIL SAFE, ALWAYS. Sleeping a busy workspace kills a live agent, a terminal
mid-command and an unsaved editor buffer, so every ambiguity resolves to
"busy":

  * a signal whose source cannot be read at all (`known=False`) blocks sleep
    outright — not "assume quiet because the probe broke";
  * `quiet_since` is clamped to when this process started watching, so a
    freshly booted server can never claim the workspace has been quiet for
    longer than it has been looking;
  * a task whose status is anything other than terminal counts as live, which
    is the same rule `ClaudeTaskManager._reconcile_status` applies.

The signals, and why each one is the right source:

  tasks        `ClaudeTaskManager.list_tasks()` — Builds and sub-agents. Live
               statuses are exactly `running` / `waiting-for-input`; a Build
               parked on a question is NOT idle, it is waiting for the human
               who is about to come back to it.
  board_runs   `BoardRunsManager.list_runs()` filtered by `boards.runs.is_live`
               — the run's own liveness predicate, which already knows that
               `status == 'running'` outlives the work (#712).
  terminals    Established TCP connections to ttyd (7681) and to the SSH
               sidecar's port. The ssh-server container shares this pod's
               network namespace, so /proc/net/tcp sees its sessions too.
  code_server  Established connections to code-server (8080). An open VS Code
               tab holds a websocket, so this is its heartbeat.
  dashboard    Established connections to the dashboard/API port (6080) from a
               NON-loopback peer. In-pod callers (kubelet probes, the MCP
               servers, `curl localhost:6080`) are loopback and deliberately
               do not count — otherwise the pod's own probes would keep it
               awake for ever. A browser or the mobile app arrives via
               oauth2-proxy or the ingress, i.e. from another pod's IP. The
               caller being served is excluded too (`exclude_peers`), which is
               what keeps the keeper's own 60s poll from reading as a user —
               and is why the keeper MUST poll the workspace Service directly
               rather than loop back through the ingress, which would make its
               peer address indistinguishable from a browser's and blind the
               signal entirely.

Connection counting reads /proc/net/tcp{,6} directly rather than shelling out
to `ss`: the keeper polls this endpoint every 60s for the life of the
workspace, and two file reads beat a subprocess every minute.
"""

import ipaddress
import os
import time

#: Ports observed for the connection-backed signals. All four listeners live in
#: this pod (ttyd and code-server in the `ide` container, sshd in the
#: `ssh-server` sidecar, the dashboard in server.py itself), and a pod shares
#: one network namespace, so one /proc read sees all of them.
TTYD_PORT = int(os.environ.get('KC_TTYD_PORT', '7681'))
CODE_SERVER_PORT = int(os.environ.get('KC_CODE_SERVER_PORT', '8080'))
DASHBOARD_PORT = int(os.environ.get('KC_DASHBOARD_PORT', '6080'))
SSH_PORT = int(os.environ.get('KC_SSH_PORT', '22'))

#: The keeper's default threshold, matching `keeper.sleep.idleMinutes`. The
#: request may override it; the endpoint reports the value it used either way.
DEFAULT_IDLE_MINUTES = int(os.environ.get('KC_KEEPER_IDLE_MINUTES', '30'))

#: A task in either of these is still somebody's work in progress. Same pair
#: `_reconcile_status` treats as not-yet-finished (server.py) — including
#: `waiting-for-input`, which is a human's turn, not an idle process.
LIVE_TASK_STATUSES = frozenset({'running', 'waiting-for-input'})

#: /proc/net/tcp state column for ESTABLISHED.
_TCP_ESTABLISHED = '01'

#: When this process started watching. `quiet_since` never predates it.
WATCHING_SINCE = time.time()


def signal(busy, *, since=None, detail=''):
    """One observation: is this source busy, and when did it last see anything?

    `since` is a wall-clock timestamp of the most recent activity the source
    can attest to, or None when it has no clock (a connection count knows
    "nobody is connected now" but not when the last one left).
    """
    return {'busy': bool(busy), 'known': True, 'since': since,
            'detail': detail}


def unknown(detail):
    """A source that could not be read. Blocks sleep — see the module docstring."""
    return {'busy': True, 'known': False, 'since': None, 'detail': detail}


# --- connection counting -----------------------------------------------------

def _parse_hex_addr(raw):
    """`/proc/net/tcp` hex address → (ip, port), or None if unparseable.

    The kernel prints the address in host byte order as one hex blob, and for
    IPv6 as four little-endian 32-bit words — so the bytes of each 4-byte group
    have to be reversed before they mean anything. A v4-mapped v6 address is
    unwrapped so `::ffff:127.0.0.1` reads as the loopback it is.
    """
    try:
        host, _, port = raw.rpartition(':')
        port = int(port, 16)
        packed = b''.join(bytes.fromhex(host[i:i + 8])[::-1]
                          for i in range(0, len(host), 8))
        ip = ipaddress.ip_address(packed)
    except ValueError:
        return None
    mapped = getattr(ip, 'ipv4_mapped', None)
    return (mapped or ip), port


def established(port, *, exclude_loopback=False, exclude_peers=(),
                proc_files=None):
    """ESTABLISHED connections to `port` in this pod, or None if /proc is unreadable.

    None rather than 0 on failure, so a broken read becomes an `unknown`
    signal instead of a silent "nobody is here".

    `exclude_peers` drops connections from those remote addresses. The caller
    that needs it is the poll itself: the keeper is a separate pod, so ITS
    request arrives non-loopback and would otherwise count as a dashboard
    client — every 60s, for ever, keeping the workspace awake on the strength
    of the question "may I put you to sleep?". Excluding the peer being served
    is what stops the signal observing its own observer.
    """
    paths = proc_files or ('/proc/net/tcp', '/proc/net/tcp6')
    skip = set()
    for peer in exclude_peers or ():
        try:
            skip.add(ipaddress.ip_address(peer))
        except ValueError:
            continue
    found = 0
    read_any = False
    for path in paths:
        try:
            with open(path, 'r') as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        read_any = True
        for line in lines[1:]:
            fields = line.split()
            if len(fields) < 4 or fields[3] != _TCP_ESTABLISHED:
                continue
            local = _parse_hex_addr(fields[1])
            remote = _parse_hex_addr(fields[2])
            if not local or not remote or local[1] != port:
                continue
            if exclude_loopback and remote[0].is_loopback:
                continue
            if remote[0] in skip:
                continue
            found += 1
    return found if read_any else None


def probe_connections(ports, *, exclude_loopback=False, exclude_peers=(),
                      label='connections', proc_files=None):
    """A signal from the live connection count on `ports`."""
    total = 0
    for port in ports:
        count = established(port, exclude_loopback=exclude_loopback,
                            exclude_peers=exclude_peers,
                            proc_files=proc_files)
        if count is None:
            return unknown('/proc/net/tcp unreadable')
        total += count
    plural = '' if total == 1 else 's'
    return signal(total > 0, detail=f'{total} {label}{plural}')


# --- task and board-run probes ----------------------------------------------

def probe_tasks(list_tasks):
    """Live Builds, plus the newest activity timestamp across ALL of them.

    Finished tasks still pin `quiet_since`: a Build that completed 30 seconds
    ago means the user was here 30 seconds ago, and they are very likely about
    to read its output.
    """
    try:
        rows = list(list_tasks() or ())
    except Exception as exc:                                    # noqa: BLE001
        return unknown(f'task listing failed: {exc.__class__.__name__}')
    live = [r for r in rows
            if isinstance(r, dict) and r.get('status') in LIVE_TASK_STATUSES]
    stamps = [_newest(r, 'last_activity_at', 'finished_at', 'created_at')
              for r in rows if isinstance(r, dict)]
    stamps = [s for s in stamps if s is not None]
    plural = '' if len(live) == 1 else 's'
    return signal(bool(live), since=max(stamps) if stamps else None,
                  detail=f'{len(live)} live task{plural} of {len(rows)}')


def probe_board_runs(list_runs, is_live):
    """Board runs actually working something right now (`boards.runs.is_live`).

    `None` for either callable means the `boards` package did not import (see
    `_BOARDS_AVAILABLE` in server.py). That is a KNOWN quiet, not an unknown:
    a workspace with no board subsystem has no board runs to wait for, and
    treating it as unreadable would make such a workspace un-sleepable for
    ever.
    """
    if list_runs is None or is_live is None:
        return signal(False, detail='board subsystem unavailable')
    try:
        rows = list(list_runs() or ())
    except Exception as exc:                                    # noqa: BLE001
        return unknown(f'run listing failed: {exc.__class__.__name__}')
    live, stamps = [], []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stamp = _newest(row, 'updated_at', 'finished_at', 'started_at',
                        'created_at')
        if stamp is not None:
            stamps.append(stamp)
        try:
            if is_live(row):
                live.append(row)
        except Exception:                                       # noqa: BLE001
            return unknown('run liveness check failed')
    plural = '' if len(live) == 1 else 's'
    return signal(bool(live), since=max(stamps) if stamps else None,
                  detail=f'{len(live)} live run{plural} of {len(rows)}')


def _newest(row, *keys):
    """The largest numeric timestamp among `keys`, or None when there is none."""
    best = None
    for key in keys:
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if best is None or value > best:
            best = value
    return best


# --- assembling the answer ---------------------------------------------------

def snapshot(*, list_tasks, list_runs, is_live, exclude_peers=(),
             proc_files=None):
    """Every signal, read once. Order is the order the reason quotes them in.

    `exclude_peers` should carry the address of whoever is asking — see
    `established`. It applies to the dashboard signal only: a caller reaching
    ttyd or code-server is a human session by definition, whereas a caller
    reaching the API might be the keeper itself.
    """
    return {
        'tasks': probe_tasks(list_tasks),
        'board_runs': probe_board_runs(list_runs, is_live),
        'terminals': probe_connections(
            (TTYD_PORT, SSH_PORT), label='terminal session',
            proc_files=proc_files),
        'code_server': probe_connections(
            (CODE_SERVER_PORT,), label='editor connection',
            proc_files=proc_files),
        # Loopback excluded: the pod's own probes and MCP servers talk to 6080
        # constantly and would otherwise pin the workspace awake for ever.
        'dashboard': probe_connections(
            (DASHBOARD_PORT,), exclude_loopback=True,
            exclude_peers=exclude_peers, label='dashboard client',
            proc_files=proc_files),
    }


def decide(signals, *, idle_minutes=None, now=None, watching_since=None):
    """Turn signals into `{idle, idle_for_s, quiet_since, reason, ...}`.

    Idle means every signal is quiet AND has been for `idle_minutes`. The two
    halves are reported separately (`busy` vs `idle_for_s`) so the keeper can
    log *why* it is not sleeping a workspace without re-deriving it.
    """
    now = time.time() if now is None else now
    minutes = DEFAULT_IDLE_MINUTES if idle_minutes is None else idle_minutes
    threshold = max(0.0, float(minutes) * 60.0)
    watching_since = WATCHING_SINCE if watching_since is None else watching_since

    blocked = [name for name, sig in signals.items() if sig.get('busy')]
    # Never claim a longer quiet stretch than this process has been watching,
    # and never let a clock-skewed future timestamp read as "quiet for -20s".
    stamps = [sig['since'] for sig in signals.values()
              if isinstance(sig.get('since'), (int, float))]
    quiet_since = min(now, max([watching_since] + stamps))
    idle_for = max(0.0, now - quiet_since)

    if blocked:
        first = blocked[0]
        detail = signals[first].get('detail') or first
        reason = f'busy: {detail}' + (f' (+{len(blocked) - 1} more)'
                                      if len(blocked) > 1 else '')
    elif idle_for < threshold:
        reason = (f'quiet for {int(idle_for)}s, needs '
                  f'{int(threshold)}s')
    else:
        reason = f'quiet for {int(idle_for)}s'

    return {
        'idle': not blocked and idle_for >= threshold,
        'idle_for_s': round(idle_for, 1),
        'quiet_since': round(quiet_since, 3),
        'idle_threshold_s': int(threshold),
        'idle_minutes': minutes,
        'busy_signals': blocked,
        'reason': reason,
        'signals': signals,
        'now': round(now, 3),
        'watching_since': round(watching_since, 3),
    }


def report(*, list_tasks, list_runs, is_live, idle_minutes=None, now=None,
           exclude_peers=(), proc_files=None):
    """`snapshot` + `decide` — what the endpoint serialises."""
    return decide(
        snapshot(list_tasks=list_tasks, list_runs=list_runs, is_live=is_live,
                 exclude_peers=exclude_peers, proc_files=proc_files),
        idle_minutes=idle_minutes, now=now)


def parse_idle_minutes(raw, default=None):
    """`?idle_minutes=` → a number, or `default` when absent or nonsense.

    A malformed override falls back to the configured default instead of
    erroring: the keeper asking a bad question must not make the workspace
    un-sleepable, nor sleepable on a value nobody set.
    """
    if raw in (None, ''):
        return DEFAULT_IDLE_MINUTES if default is None else default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_IDLE_MINUTES if default is None else default
    if value < 0 or value != value or value in (float('inf'), float('-inf')):
        return DEFAULT_IDLE_MINUTES if default is None else default
    return int(value) if value == int(value) else value
