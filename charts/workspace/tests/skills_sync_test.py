"""Unit tests for skills/sync.py — the background scanner + cache.

Covers: cache population via trigger_sync, snapshot isolation, variant
lookup, the mtime-fingerprint skip (no re-parse when nothing changed),
aggregate `skills.changed` publication (once per changed pass, none on
quiet passes), and the TOCTOU-safe double-start guard.

Uses a fake in-memory provider — no filesystem, no threads except the
double-start test.

Run with:    python3 -m unittest tests.skills_sync_test
(from charts/workspace/)
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import skills.sync as sync_mod  # noqa: E402
from skills.sync import SkillsSyncer, scan_once  # noqa: E402
from skills.model import SkillRecord, SkillSource, fingerprint  # noqa: E402


class FakeProvider:
    """In-memory provider with controllable records + mtimes."""
    key = 'fake'
    enabled = True

    def __init__(self):
        self.files = {}          # path -> (body, mtime)
        self.scan_calls = 0

    def set_file(self, path, body, mtime):
        self.files[path] = (body, mtime)

    def scan(self):
        self.scan_calls += 1
        out = []
        for path, (body, mtime) in sorted(self.files.items()):
            name = os.path.basename(path).replace('.md', '')
            out.append(SkillRecord(
                name=name, body=body, scope='user', systems=[self.key],
                sources=[SkillSource(system=self.key, path=path,
                                     scope='user', updated_at=mtime)],
                fingerprint=fingerprint(body), updated_at=mtime,
            ))
        return out

    def roots_mtime_fingerprint(self):
        return {p: m for p, (_b, m) in self.files.items()}


class SyncerTestBase(unittest.TestCase):
    def setUp(self):
        SkillsSyncer._reset_for_test()
        self.provider = FakeProvider()
        self._saved_providers = sync_mod.PROVIDERS
        sync_mod.PROVIDERS = {'fake': self.provider}
        self.published = []
        SkillsSyncer._publish = lambda t, d: self.published.append((t, d))

    def tearDown(self):
        sync_mod.PROVIDERS = self._saved_providers
        SkillsSyncer._reset_for_test()


class TriggerSyncTests(SyncerTestBase):
    def test_populates_cache(self):
        self.provider.set_file('/x/alpha.md', 'body A', 1.0)
        res = SkillsSyncer.trigger_sync()
        self.assertEqual(res['scanned'], 1)
        self.assertTrue(res['changed'])
        snap = SkillsSyncer.snapshot()
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0].name, 'alpha')

    def test_snapshot_is_a_copy(self):
        self.provider.set_file('/x/alpha.md', 'body', 1.0)
        SkillsSyncer.trigger_sync()
        snap = SkillsSyncer.snapshot()
        snap.clear()
        self.assertEqual(len(SkillsSyncer.snapshot()), 1)

    def test_get_returns_variants(self):
        self.provider.set_file('/x/alpha.md', 'body', 1.0)
        SkillsSyncer.trigger_sync()
        self.assertEqual(len(SkillsSyncer.get('alpha')), 1)
        self.assertEqual(SkillsSyncer.get('nope'), [])

    def test_scan_once_survives_provider_error(self):
        class Boom:
            key = 'boom'
            enabled = True

            def scan(self):
                raise RuntimeError('kaboom')

            def roots_mtime_fingerprint(self):
                return {}

        sync_mod.PROVIDERS = {'boom': Boom(), 'fake': self.provider}
        self.provider.set_file('/x/ok.md', 'fine', 1.0)
        out = scan_once()
        self.assertEqual(len(out), 1)   # good provider still contributes


class EventTests(SyncerTestBase):
    def test_change_publishes_once(self):
        self.provider.set_file('/x/alpha.md', 'body', 1.0)
        SkillsSyncer.trigger_sync()
        events = [e for e in self.published if e[0] == 'skills.changed']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1]['count'], 1)

    def test_no_change_no_publish(self):
        self.provider.set_file('/x/alpha.md', 'body', 1.0)
        SkillsSyncer.trigger_sync()
        self.published.clear()
        SkillsSyncer.trigger_sync()   # identical content
        self.assertEqual(self.published, [])

    def test_publish_failure_is_swallowed(self):
        def bad(_t, _d):
            raise RuntimeError('broker down')
        SkillsSyncer._publish = bad
        self.provider.set_file('/x/alpha.md', 'body', 1.0)
        SkillsSyncer.trigger_sync()   # must not raise


class MtimeSkipTests(SyncerTestBase):
    def test_pass_skips_parse_when_unchanged(self):
        self.provider.set_file('/x/alpha.md', 'body', 1.0)
        SkillsSyncer._pass()
        self.assertEqual(self.provider.scan_calls, 1)
        SkillsSyncer._pass()          # same mtimes → no scan
        self.assertEqual(self.provider.scan_calls, 1)

    def test_pass_rescans_on_mtime_change(self):
        self.provider.set_file('/x/alpha.md', 'body', 1.0)
        SkillsSyncer._pass()
        self.provider.set_file('/x/alpha.md', 'body v2', 2.0)
        SkillsSyncer._pass()
        self.assertEqual(self.provider.scan_calls, 2)
        self.assertIn('body v2', SkillsSyncer.snapshot()[0].body)

    def test_pass_rescans_on_new_file(self):
        self.provider.set_file('/x/alpha.md', 'body', 1.0)
        SkillsSyncer._pass()
        self.provider.set_file('/x/beta.md', 'other', 1.0)
        SkillsSyncer._pass()
        self.assertEqual(len(SkillsSyncer.snapshot()), 2)


class StartGuardTests(SyncerTestBase):
    def test_double_start_spawns_one_thread(self):
        SkillsSyncer.start(interval_seconds=3600)
        t1 = SkillsSyncer._thread
        SkillsSyncer.start(interval_seconds=3600)
        self.assertIs(SkillsSyncer._thread, t1)

    def test_status_shape(self):
        st = SkillsSyncer.status()
        self.assertIn('running', st)
        self.assertIn('count', st)
        self.assertIn('providers', st)


if __name__ == '__main__':
    unittest.main()
