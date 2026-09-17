"""Proof that the tests which used to page a real phone no longer can (#685).

`WaitingQuiescenceTests` wrote the live Feed and pushed "Task waiting on you:
tq-1" to the workspace's phone on every run — 193 times in two months — and
`feed_test.py` pushed too, because it redirected the feed files but not the
push token store. Both now call `live_state.isolate_feed_and_push`.

This guard stands up a fake "live" workspace — the module-level default paths,
with a phone registered in it — runs those suites against it, and asserts the
live Feed was never written and nothing reached Expo. A control case runs one
deliberately un-isolated emit first, so the guard cannot pass by detecting
nothing.

Run:  python3 -m unittest tests.live_state_isolation_test   (from charts/workspace/)
"""

import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import push_notify as pn  # noqa: E402
import server  # noqa: E402

import feed_test  # noqa: E402
import server_test  # noqa: E402

FM = server.FeedManager


class LeakyTestsStayIsolated(unittest.TestCase):

    def setUp(self):
        live = tempfile.mkdtemp(prefix='kctest-fake-live-')
        self.addCleanup(shutil.rmtree, live, True)
        self.live_items = os.path.join(live, 'feed', 'items.jsonl')
        for target, attr, value in (
            (FM, 'FEED_DIR', os.path.join(live, 'feed')),
            (FM, 'ITEMS_PATH', self.live_items),
            (FM, 'STATE_PATH', os.path.join(live, 'feed', 'state.json')),
            (pn, 'PUSH_DIR', os.path.join(live, 'push')),
            (pn, 'TOKENS_PATH', os.path.join(live, 'push', 'tokens.json')),
            (pn, 'SENT_PATH', os.path.join(live, 'push', 'sent.json')),
            (pn, 'PUSH_ENABLED', True),
        ):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        # The workspace owner's phone.
        pn.PushTokenStore.register('ExponentPushToken[live-phone]', 'ios', 'api:owner')
        # Anything that reaches Expo while this is the live config is a page.
        self.pages = []
        p = mock.patch.object(pn, '_post_expo', side_effect=self._page)
        p.start()
        self.addCleanup(p.stop)

    def _page(self, messages):
        self.pages.append(messages)
        return {'data': [{'status': 'ok'} for _ in messages]}

    def _settle(self):
        time.sleep(0.3)  # delivery runs on daemon threads

    def test_control_an_unisolated_emit_is_detected(self):
        with mock.patch.object(server.EventBroker, 'publish'):
            FM.emit('activity', 'Task waiting on you: control', waiting=True,
                    dedupe_key='task:control:waiting')
        self._settle()
        self.assertTrue(os.path.exists(self.live_items))
        self.assertEqual(len(self.pages), 1)

    def test_formerly_leaky_suites_touch_neither_the_feed_nor_the_phone(self):
        loader = unittest.defaultTestLoader
        suite = unittest.TestSuite()
        for cls in (server_test.WaitingQuiescenceTests,
                    server_test.CompletionHookTests,
                    server_test.WebhookReceiverTests,
                    feed_test.EmitAndListTests,
                    feed_test.SystemEmitterTests,
                    feed_test.FeedHandlerTests):
            suite.addTests(loader.loadTestsFromTestCase(cls))
        out = io.StringIO()
        result = unittest.TextTestRunner(stream=out, verbosity=0).run(suite)
        self._settle()
        self.assertTrue(result.wasSuccessful(), out.getvalue())
        self.assertGreater(result.testsRun, 20)
        self.assertFalse(os.path.exists(self.live_items),
                         'a test wrote the live Feed log')
        self.assertEqual(self.pages, [], 'a test pushed to the live phone')


class MakefileSafetyNetTests(unittest.TestCase):
    """The suite-wide second line of defence: `make python-tests` runs with the
    feed and push dirs pointed at a throwaway folder, so a future test that
    forgets the helper still cannot reach a phone."""

    def _recipe(self, target):
        with open(os.path.join(REPO, 'Makefile'), encoding='utf-8') as f:
            lines = f.read().splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.startswith(target + ':'))
        body = []
        for ln in lines[start + 1:]:
            if not ln.startswith('\t'):
                break
            body.append(ln)
        return '\n'.join(body)

    def test_test_targets_isolate_feed_and_push(self):
        for target in ('python-tests', 'python-coverage'):
            recipe = self._recipe(target)
            with self.subTest(target=target):
                self.assertIn('KC_FEED_DIR=', recipe)
                self.assertIn('KC_PUSH_DIR=', recipe)
                self.assertIn('mktemp -d', recipe)


if __name__ == '__main__':
    unittest.main()
