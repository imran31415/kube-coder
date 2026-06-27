"""Unit tests for playwright_reaper.py — the idle/orphan browser reaper.

Drives the pure decision logic with synthetic process tables (no /proc, no
real signals) to verify the issue-143 acceptance criteria:

  - an idle Playwright browser is killed within a bounded window;
  - an actively-driven browser is never killed;
  - a crash-orphaned (ppid=1) Playwright Firefox is reaped — the gap the
    old chromium-only bash reaper had;
  - the legacy generic-chromium orphan reap still fires;
  - a user's own (non-Playwright) Firefox is left alone.
"""

from __future__ import annotations

import os
import signal
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import playwright_reaper as pr  # noqa: E402
from playwright_reaper import Proc  # noqa: E402

# A representative Playwright Firefox cmdline (root) and a content child.
FF_ROOT_ARGS = ('/home/dev/.cache/ms-playwright/firefox-1489/firefox/firefox '
                '-no-remote -profile /tmp/pw_tmp -juggler-pipe')
FF_CHILD_ARGS = ('/home/dev/.cache/ms-playwright/firefox-1489/firefox/firefox '
                 '-contentproc -childID 2 -isForBrowser')
CHROMIUM_ROOT_ARGS = ('/home/dev/.cache/ms-playwright/chromium-1140/chrome-linux/'
                      'chrome --remote-debugging-port=0 --headless')
# A user's system Firefox launched on the VNC display — must be ignored.
USER_FF_ARGS = '/usr/lib/firefox/firefox'

CLK = pr.CLK_TCK


def ff_session(root_pid=1000, child_pid=1001, ppid=900, root_cpu=500,
               child_cpu=500, starttime=111):
    """A two-process Playwright Firefox tree (root + one content child)."""
    return [
        Proc(root_pid, ppid, starttime, root_cpu, 'firefox', FF_ROOT_ARGS),
        Proc(child_pid, root_pid, starttime + 1, child_cpu, 'Web Content',
             FF_CHILD_ARGS),
    ]


def collect(actions_kill_log):
    """Flatten a kill-capture into {pid: signal}."""
    out = {}
    for pids, sig in actions_kill_log:
        for pid in pids:
            out[pid] = sig
    return out


class FakeKill:
    def __init__(self):
        self.calls = []

    def __call__(self, pids, sig):
        self.calls.append((list(pids), sig))

    def killed(self):
        return collect(self.calls)


class ClassificationTests(unittest.TestCase):
    def test_identifies_playwright_firefox_root(self):
        p = Proc(1000, 900, 111, 0, 'firefox', FF_ROOT_ARGS)
        self.assertTrue(pr.is_playwright_root(p))

    def test_ignores_user_system_firefox(self):
        p = Proc(1000, 900, 111, 0, 'firefox', USER_FF_ARGS)
        self.assertFalse(pr.is_playwright_root(p))
        self.assertFalse(pr.is_generic_chromium_orphan(p))

    def test_content_child_is_not_a_root(self):
        p = Proc(1001, 1000, 112, 0, 'Web Content', FF_CHILD_ARGS)
        self.assertFalse(pr.is_playwright_root(p))

    def test_generic_chromium_orphan_only_at_ppid1(self):
        child = Proc(2000, 1500, 200, 0, 'chrome', '/usr/bin/chrome --foo')
        orphan = Proc(2000, 1, 200, 0, 'chrome', '/usr/bin/chrome --foo')
        self.assertFalse(pr.is_generic_chromium_orphan(child))
        self.assertTrue(pr.is_generic_chromium_orphan(orphan))

    def test_playwright_chromium_not_treated_as_generic_orphan(self):
        # ppid=1 playwright chromium is handled by the session path, not the
        # generic path (which would otherwise double-count it).
        p = Proc(2000, 1, 200, 0, 'chrome', CHROMIUM_ROOT_ARGS)
        self.assertFalse(pr.is_generic_chromium_orphan(p))
        self.assertTrue(pr.is_playwright_root(p))


class SessionTests(unittest.TestCase):
    def test_tree_cpu_summed_across_children(self):
        sessions = pr.build_sessions(ff_session(root_cpu=300, child_cpu=700))
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]['cpu'], 1000)
        self.assertCountEqual(sessions[0]['pids'], [1000, 1001])
        self.assertFalse(sessions[0]['orphan'])

    def test_orphan_flag_set_when_reparented(self):
        sessions = pr.build_sessions(ff_session(ppid=1))
        self.assertTrue(sessions[0]['orphan'])


class IdleSweepTests(unittest.TestCase):
    def test_idle_browser_killed_after_threshold(self):
        """A browser burning ~no CPU is TERMed after IDLE_SWEEPS sweeps."""
        state = {'sessions': {}}
        now = 1000.0
        interval = 300.0  # 5 min
        killed = None
        # Same CPU every sweep => fully idle.
        for i in range(pr.IDLE_SWEEPS + 1):
            procs = ff_session(root_cpu=500, child_cpu=500)
            fk = FakeKill()
            state, logs = pr.sweep(procs, state, now, kill=fk)
            now += interval
            if fk.calls:
                killed = fk.killed()
                break
        self.assertIsNotNone(killed, 'idle browser was never killed')
        self.assertEqual(killed.get(1000), signal.SIGTERM)
        self.assertEqual(killed.get(1001), signal.SIGTERM)

    def test_idle_kill_lands_within_one_hour(self):
        """Acceptance: idle session dies well within the 1-hour bound."""
        state = {'sessions': {}}
        now = 0.0
        interval = 300.0
        kill_time = None
        for i in range(20):
            fk = FakeKill()
            state, _ = pr.sweep(ff_session(), state, now, kill=fk)
            if fk.calls and kill_time is None:
                kill_time = now
                break
            now += interval
        self.assertIsNotNone(kill_time)
        self.assertLessEqual(kill_time, 3600)

    def test_active_browser_never_killed(self):
        """A browser accumulating real CPU each sweep is never killed."""
        state = {'sessions': {}}
        now = 1000.0
        interval = 300.0
        cpu = 500
        for i in range(pr.IDLE_SWEEPS + 5):
            # Burn ~50% of a core over the interval — clearly active.
            cpu += int(0.5 * interval * CLK)
            procs = ff_session(root_cpu=cpu, child_cpu=cpu)
            fk = FakeKill()
            state, _ = pr.sweep(procs, state, now, kill=fk)
            self.assertEqual(fk.calls, [], 'active browser killed at sweep %d' % i)
            now += interval

    def test_activity_resets_idle_counter(self):
        """Idle accumulation is cleared by a burst, deferring any kill."""
        state = {'sessions': {}}
        now = 1000.0
        interval = 300.0
        cpu = 500
        # Idle for a few sweeps (below threshold)...
        for _ in range(pr.IDLE_SWEEPS - 1):
            fk = FakeKill()
            state, _ = pr.sweep(ff_session(root_cpu=cpu, child_cpu=cpu),
                                state, now, kill=fk)
            self.assertEqual(fk.calls, [])
            now += interval
        # ...then a burst of activity resets the counter.
        cpu += int(0.9 * interval * CLK)
        fk = FakeKill()
        state, _ = pr.sweep(ff_session(root_cpu=cpu, child_cpu=cpu),
                            state, now, kill=fk)
        self.assertEqual(fk.calls, [])
        now += interval
        # Next idle sweep must not immediately kill (counter restarted at 1).
        fk = FakeKill()
        state, _ = pr.sweep(ff_session(root_cpu=cpu, child_cpu=cpu),
                            state, now, kill=fk)
        self.assertEqual(fk.calls, [])

    def test_term_escalates_to_kill_if_session_lingers(self):
        """A session that survives TERM is SIGKILLed on the next sweep."""
        state = {'sessions': {}}
        now = 1000.0
        interval = 300.0
        last_sig = None
        for i in range(pr.IDLE_SWEEPS + 3):
            fk = FakeKill()
            state, _ = pr.sweep(ff_session(root_cpu=500, child_cpu=500),
                                state, now, kill=fk)
            if fk.calls:
                last_sig = fk.killed().get(1000)
            now += interval
        self.assertEqual(last_sig, signal.SIGKILL)


class OrphanReapTests(unittest.TestCase):
    def test_orphan_playwright_firefox_reaped_immediately(self):
        """The gap the old reaper had: ppid=1 Playwright Firefox."""
        fk = FakeKill()
        procs = ff_session(ppid=1)
        pr.sweep(procs, {'sessions': {}}, 1000.0, kill=fk)
        killed = fk.killed()
        self.assertEqual(killed.get(1000), signal.SIGTERM)
        self.assertEqual(killed.get(1001), signal.SIGTERM)

    def test_generic_chromium_orphan_still_reaped(self):
        fk = FakeKill()
        orphan = Proc(3000, 1, 300, 0, 'chrome', '/usr/bin/chrome --headless')
        pr.sweep([orphan], {'sessions': {}}, 1000.0, kill=fk)
        self.assertEqual(fk.killed().get(3000), signal.SIGTERM)

    def test_user_firefox_left_alone(self):
        """A user's VNC Firefox (even if orphaned) is never reaped."""
        fk = FakeKill()
        user_ff = Proc(4000, 1, 400, 999999, 'firefox', USER_FF_ARGS)
        next_state, logs = pr.sweep([user_ff], {'sessions': {}}, 1000.0, kill=fk)
        self.assertEqual(fk.calls, [])
        self.assertEqual(logs, [])


class StateIoTests(unittest.TestCase):
    def test_state_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'state.json')
            self.assertEqual(pr.load_state(path), {'sessions': {}})
            pr.save_state(path, {'sessions': {'1:2': {'cpu': 5}}})
            self.assertEqual(pr.load_state(path),
                             {'sessions': {'1:2': {'cpu': 5}}})

    def test_load_state_tolerates_garbage(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'state.json')
            with open(path, 'w') as f:
                f.write('not json{')
            self.assertEqual(pr.load_state(path), {'sessions': {}})


if __name__ == '__main__':
    unittest.main()
