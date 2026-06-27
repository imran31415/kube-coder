#!/usr/bin/env python3
"""Reap leaked / idle Playwright MCP browser processes.

Long-lived Playwright MCP browsers (we drive Firefox) accumulate in
workspace pods and hold ~1.3 GB of RAM indefinitely: the MCP server keeps
the browser open for the lifetime of the Claude session and does not close
it when the task that needed it finishes. On 4 GB-limited workspaces a
parked browser drifts the `ide` container toward its limit and contributes
to OOM kills. See https://github.com/imran31415/kube-coder/issues/143.

`@playwright/mcp` exposes no idle/session-timeout flag (verified against
`@latest`), so lifecycle management has to live outside the tool. This
helper is invoked from the start.sh supervision loop every ~5 minutes and:

  1. **Reaps crash-orphans** — a Playwright browser whose MCP/node parent
     died is reparented to PID 1; it is abandoned by definition, so it is
     killed immediately. This closes the Firefox-coverage gap in the old
     bash reaper, which only matched chromium/headless_shell.

  2. **Sweeps idle sessions** — it tracks each live Playwright browser
     tree's accumulated CPU time across invocations (state in /tmp). A tree
     that has burned negligible CPU for `KC_PW_IDLE_SWEEPS` consecutive
     sweeps (~30 min by default) is treated as abandoned and killed
     (SIGTERM, escalating to SIGKILL if it lingers). Any real activity
     resets the counter, so a browser that is actively driven mid-task is
     never killed. The MCP server stays up and lazily relaunches a fresh
     browser on the next browser tool call — the same recovery behaviour as
     today.

  3. **Preserves the legacy generic-chromium orphan reap** — PID 1 orphan
     chromium/headless_shell (or anything with --remote-debugging-port)
     from a crashed session is still killed, matching prior behaviour.

Deliberately *not* killed: a user's own Firefox launched on the VNC display
(`firefox &` on :99) — that binary lives at /usr/lib/firefox, not under
ms-playwright, so the Playwright matchers never select it. Only orphan
*chromium* is reaped generically (parity with the old reaper); orphan
*firefox* is reaped only when it is a Playwright browser.

Pure-stdlib and importable: the decision logic is separated from /proc and
os.kill so it can be unit-tested with synthetic process tables. Run
directly (`python3 playwright_reaper.py`) for one sweep.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import tempfile
import time

# --- Tunables (env-overridable so ops can adjust without a rebuild) --------

# A Playwright browser tree must look idle for this many consecutive sweeps
# before it is killed. The supervision loop sweeps every ~5 min, so the
# default of 6 is ~30 min idle — comfortably under the issue's 1-hour
# acceptance bound while leaving slack so an actively driven browser (which
# bursts CPU on every navigate/click/snapshot) never reaches the threshold.
IDLE_SWEEPS = max(1, int(os.environ.get('KC_PW_IDLE_SWEEPS', '6')))

# Average CPU usage (as a fraction of one core) below which a browser tree
# is considered idle for a sweep. An idle Firefox still ticks timers/GC, so
# this is not zero; an in-use browser sits well above it. 0.02 == 2%.
IDLE_CPU_FRACTION = float(os.environ.get('KC_PW_IDLE_CPU_FRACTION', '0.02'))

# Where per-session CPU/idle counters persist between sweeps. /tmp is
# ephemeral, which is correct: on pod restart the browsers are gone too, so
# resetting the counters loses nothing.
STATE_PATH = os.environ.get('KC_PW_REAPER_STATE', '/tmp/.playwright-reaper-state.json')

CLK_TCK = os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 100

# comm (truncated to 15 chars by the kernel) of a *main* browser process.
# Firefox/Chromium content & utility children do not carry these names
# (they show up as "Web Content", "Isolated Web Co", etc.), so matching on
# comm selects exactly the session roots; the children are then gathered by
# walking the process tree.
_BROWSER_ROOT_COMMS = {'firefox', 'chrome', 'chromium', 'headless_shell'}

# A Playwright-managed browser binary lives under the ms-playwright browser
# cache. This is what distinguishes it from a user's system Firefox.
_PLAYWRIGHT_PATH_RE = re.compile(r'ms-playwright/[^\0 ]*'
                                 r'(firefox|chrome|chromium|headless_shell)')

# Legacy generic orphan match (non-Playwright): a chromium-family browser or
# anything exposing a remote-debugging port. Firefox is intentionally absent
# here so a user's VNC Firefox is never reaped generically.
_GENERIC_CHROMIUM_COMMS = {'chrome', 'chromium', 'headless_shell'}


class Proc:
    """A minimal snapshot of one process, sourced from /proc or a test."""

    __slots__ = ('pid', 'ppid', 'starttime', 'cpu', 'comm', 'args')

    def __init__(self, pid, ppid, starttime, cpu, comm, args):
        self.pid = pid
        self.ppid = ppid
        self.starttime = starttime  # jiffies since boot; stabilises the key
        self.cpu = cpu              # utime+stime in jiffies
        self.comm = comm
        self.args = args            # full cmdline, NULs -> spaces

    def key(self):
        return '%d:%d' % (self.pid, self.starttime)


# --- /proc reading ---------------------------------------------------------

def _read_proc_one(pid):
    """Build a Proc for one pid, or None if it vanished / is unreadable."""
    try:
        with open('/proc/%d/stat' % pid, 'rb') as f:
            raw = f.read().decode('utf-8', 'replace')
        # comm is field 2, wrapped in parens, and may itself contain spaces
        # or ')'. Split around the LAST ')': everything after it is the
        # space-separated fields 3.. with no embedded parens.
        lparen = raw.index('(')
        rparen = raw.rindex(')')
        comm = raw[lparen + 1:rparen]
        rest = raw[rparen + 2:].split()
        # rest[i] is stat field (i + 3).
        ppid = int(rest[1])        # field 4
        utime = int(rest[11])      # field 14
        stime = int(rest[12])      # field 15
        starttime = int(rest[19])  # field 22
    except (FileNotFoundError, ProcessLookupError, ValueError, IndexError):
        return None
    except OSError:
        return None
    try:
        with open('/proc/%d/cmdline' % pid, 'rb') as f:
            args = f.read().replace(b'\0', b' ').decode('utf-8', 'replace').strip()
    except OSError:
        args = ''
    return Proc(pid, ppid, starttime, utime + stime, comm, args)


def read_processes():
    """Snapshot every readable process on the system."""
    procs = []
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        p = _read_proc_one(int(name))
        if p is not None:
            procs.append(p)
    return procs


# --- classification --------------------------------------------------------

def is_playwright_root(p):
    """True for a *main* Playwright browser process (a session root)."""
    return p.comm in _BROWSER_ROOT_COMMS and 'ms-playwright' in p.args \
        and _PLAYWRIGHT_PATH_RE.search(p.args) is not None


def is_generic_chromium_orphan(p):
    """True for a non-Playwright PID-1 orphan chromium (legacy behaviour)."""
    if p.ppid != 1:
        return False
    if 'ms-playwright' in p.args:
        return False  # handled by the Playwright path
    return p.comm in _GENERIC_CHROMIUM_COMMS or '--remote-debugging-port' in p.args


def _descendants(root_pid, children):
    """All pids in the subtree rooted at root_pid (excluding the root)."""
    out = []
    stack = list(children.get(root_pid, ()))
    while stack:
        pid = stack.pop()
        out.append(pid)
        stack.extend(children.get(pid, ()))
    return out


def build_sessions(procs):
    """Group Playwright processes into sessions keyed by their root.

    Returns a list of dicts: {root, pids, cpu, orphan} where `pids` is the
    whole tree (root + descendants), `cpu` is the summed CPU jiffies, and
    `orphan` flags a crash-orphan (root reparented to PID 1).
    """
    by_pid = {p.pid: p for p in procs}
    children = {}
    for p in procs:
        children.setdefault(p.ppid, []).append(p.pid)

    sessions = []
    for p in procs:
        if not is_playwright_root(p):
            continue
        # Skip a root whose parent is itself a Playwright root: it is a child
        # window, accounted for under its ancestor's tree.
        parent = by_pid.get(p.ppid)
        if parent is not None and is_playwright_root(parent):
            continue
        tree_pids = [p.pid] + _descendants(p.pid, children)
        cpu = sum(by_pid[pid].cpu for pid in tree_pids if pid in by_pid)
        sessions.append({
            'root': p,
            'pids': tree_pids,
            'cpu': cpu,
            'orphan': p.ppid == 1,
        })
    return sessions


# --- decision --------------------------------------------------------------

def decide(sessions, generic_orphans, prev_state, now):
    """Decide what to kill this sweep and compute the next state.

    Pure function (no I/O) so it is unit-testable. Returns
    (actions, next_state, logs) where actions is a list of
    {pids, signal, reason}.
    """
    actions = []
    logs = []
    next_sessions = {}
    prev_sessions = (prev_state or {}).get('sessions', {})

    for sess in sessions:
        root = sess['root']
        key = root.key()

        if sess['orphan']:
            actions.append({
                'pids': sess['pids'],
                'signal': signal.SIGTERM,
                'reason': 'orphan Playwright browser (ppid=1) pid=%d' % root.pid,
            })
            logs.append('reaping orphan Playwright browser tree root=%d (%d procs)'
                        % (root.pid, len(sess['pids'])))
            continue  # don't carry killed sessions into next state

        prev = prev_sessions.get(key)
        idle_ticks = 0
        if prev is not None:
            elapsed = now - prev.get('ts', now)
            delta_cpu = sess['cpu'] - prev.get('cpu', sess['cpu'])
            # Negative delta (counter reset / pid reuse) counts as activity.
            if elapsed > 0 and delta_cpu >= 0:
                busy_fraction = delta_cpu / (elapsed * CLK_TCK)
            else:
                busy_fraction = 1.0
            if busy_fraction < IDLE_CPU_FRACTION:
                idle_ticks = int(prev.get('idle_ticks', 0)) + 1
            else:
                idle_ticks = 0

        if idle_ticks == IDLE_SWEEPS:
            actions.append({
                'pids': sess['pids'],
                'signal': signal.SIGTERM,
                'reason': 'idle Playwright browser root=%d (%d idle sweeps)'
                          % (root.pid, idle_ticks),
            })
            logs.append('TERM idle Playwright browser tree root=%d after %d idle sweeps'
                        % (root.pid, idle_ticks))
        elif idle_ticks > IDLE_SWEEPS:
            actions.append({
                'pids': sess['pids'],
                'signal': signal.SIGKILL,
                'reason': 'idle Playwright browser survived TERM root=%d' % root.pid,
            })
            logs.append('KILL idle Playwright browser tree root=%d (survived TERM)'
                        % root.pid)

        # Carry the session forward so its CPU baseline / counter persists.
        next_sessions[key] = {
            'cpu': sess['cpu'],
            'ts': now,
            'idle_ticks': idle_ticks,
        }

    for p in generic_orphans:
        actions.append({
            'pids': [p.pid],
            'signal': signal.SIGTERM,
            'reason': 'orphan chromium (ppid=1) pid=%d' % p.pid,
        })
        logs.append('reaping orphan browser PID %d' % p.pid)

    return actions, {'sessions': next_sessions}, logs


# --- state I/O -------------------------------------------------------------

def load_state(path):
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, ValueError, OSError):
        pass
    return {'sessions': {}}


def save_state(path, state):
    parent = os.path.dirname(path) or '.'
    try:
        fd, tmp = tempfile.mkstemp(prefix='.pw-reaper-', dir=parent)
        with os.fdopen(fd, 'w') as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _kill(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


# --- entrypoint ------------------------------------------------------------

def sweep(procs, prev_state, now, kill=_kill):
    """Run one sweep over a process snapshot. Returns (next_state, logs)."""
    sessions = build_sessions(procs)
    generic = [p for p in procs if is_generic_chromium_orphan(p)]
    actions, next_state, logs = decide(sessions, generic, prev_state, now)
    for act in actions:
        kill(act['pids'], act['signal'])
    return next_state, logs


def main():
    procs = read_processes()
    state = load_state(STATE_PATH)
    next_state, logs = sweep(procs, state, time.time())
    save_state(STATE_PATH, next_state)
    for line in logs:
        print('[playwright_reaper] ' + line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
