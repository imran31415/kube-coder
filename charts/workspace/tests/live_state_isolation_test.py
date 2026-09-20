"""Proof that the suite cannot reach this workspace's own state (#685, #712).

`WaitingQuiescenceTests` wrote the live Feed and pushed "Task waiting on you:
tq-1" to the workspace's phone on every run — 193 times in two months — and
`feed_test.py` pushed too, because it redirected the feed files but not the
push token store. Both now call `live_state.isolate_feed_and_push`.

This guard stands up a fake "live" workspace — the module-level default paths,
with a phone registered in it — runs those suites against it, and asserts the
live Feed was never written and nothing reached Expo. A control case runs one
deliberately un-isolated emit first, so the guard cannot pass by detecting
nothing.

The same shape covers the OTHER live store a test can reach: the API keys the
user set in Settings. `available_assistants()` gates the assistant list on
them, so on a workspace whose owner has an OpenRouter key two assistant
assertions failed that pass in CI — the suite was reading the developer's
keys. `ProviderKeysManagerStaysIsolated` stands up a fake live store with a
key in it and runs those tests against it.

Run:  python3 -m unittest tests.live_state_isolation_test   (from charts/workspace/)
"""

import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import hypervisor_session  # noqa: E402
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


class ProviderKeysStayIsolated(unittest.TestCase):
    """The assistant tests must read the ENV, never the workspace's keys."""

    def setUp(self):
        live = tempfile.mkdtemp(prefix='kctest-fake-keys-')
        self.addCleanup(shutil.rmtree, live, True)
        self.live_keys = os.path.join(live, 'provider-keys.json')
        # The workspace owner's stored key — the exact thing that broke these.
        with open(self.live_keys, 'w') as f:
            f.write('{"OPENROUTER_API_KEY": "sk-or-live-owner-key"}')
        for target, attr in ((server.ProviderKeysManager, 'KEYS_FILE'),
                             (hypervisor_session, '_PROVIDER_KEYS_FILE')):
            p = mock.patch.object(target, attr, self.live_keys)
            p.start()
            self.addCleanup(p.stop)

    def test_control_an_unisolated_read_sees_the_owners_key(self):
        # Without this the guard below could pass by measuring nothing.
        self.assertEqual(
            server.ClaudeTaskManager._provider_keys().get('OPENROUTER_API_KEY'),
            'sk-or-live-owner-key')
        self.assertIn('opencode-openrouter',
                      [a['id'] for a in
                       server.ClaudeTaskManager.available_assistants()])

    def test_the_assistant_suite_passes_against_a_keyed_workspace(self):
        loader = unittest.defaultTestLoader
        suite = loader.loadTestsFromTestCase(
            server_test.AssistantSelectionTests)
        out = io.StringIO()
        result = unittest.TextTestRunner(stream=out, verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful(), out.getvalue())
        self.assertGreater(result.testsRun, 20)

    def test_nothing_in_that_suite_rewrote_the_owners_keys(self):
        # The write direction is the worse one: `ProviderKeysManager.set` in a
        # test that forgot to redirect KEYS_FILE overwrites real API keys.
        with open(self.live_keys) as f:
            before = f.read()
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            server_test.AssistantSelectionTests)
        unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        with open(self.live_keys) as f:
            self.assertEqual(f.read(), before)


class LeakedThreadTests(unittest.TestCase):
    """A test that creates a task must not leave a thread shelling out to the
    developer's own tmux after it finishes.

    `create_task` spawns a prompt-delivery thread. In a test the pane never
    exists, and by the time the thread polls, the test's
    `mock.patch('server.subprocess.run')` is gone — so the calls were real.
    Nine of them survived `server_test` alone, each polling for the full
    45-second ceiling."""

    @staticmethod
    def _prompt_threads():
        return {t for t in threading.enumerate() if 'send_prompt' in t.name}

    def test_the_task_creating_suites_leave_no_prompt_threads_behind(self):
        # Compared against a SNAPSHOT, not against zero: this runs inside a
        # whole-suite process, and a thread some other module leaked is that
        # module's bug to report, not this assertion's.
        before = self._prompt_threads()
        suite = unittest.TestSuite()
        loader = unittest.defaultTestLoader
        for cls in (server_test.CompletionHookTests,
                    server_test.AssistantSelectionTests,
                    server_test.WebhookReceiverTests):
            suite.addTests(loader.loadTestsFromTestCase(cls))
        out = io.StringIO()
        result = unittest.TextTestRunner(stream=out, verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful(), out.getvalue())
        time.sleep(0.2)
        leaked = [t.name for t in self._prompt_threads() - before]
        self.assertEqual(leaked, [], 'a prompt-delivery thread outlived its test')

    def test_the_wait_gives_up_once_the_session_reads_as_gone(self):
        # The bound that makes any future leak survivable: a failed capture is
        # tmux saying "no such session", not a pane that is still drawing.
        calls = []
        with mock.patch.object(server.ClaudeTaskManager, '_capture_pane',
                               side_effect=lambda s: calls.append(s)), \
             mock.patch.object(server.time, 'sleep'):
            server.ClaudeTaskManager._wait_for_pane_ready(
                'gone', floor=0, ceiling=45, interval=0, expect_composer=True)
        self.assertEqual(len(calls),
                         server.ClaudeTaskManager.PANE_GONE_STRIKES)


class MakefileSafetyNetTests(unittest.TestCase):
    """The suite-wide second line of defence: `make python-tests` runs with the
    feed, push and provider-key paths pointed at a throwaway folder, so a
    future test that forgets the helper still cannot reach a phone or read the
    user's API keys."""

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

    def test_test_targets_isolate_every_live_store(self):
        for target in ('python-tests', 'python-coverage'):
            recipe = self._recipe(target)
            with self.subTest(target=target):
                self.assertIn('KC_FEED_DIR=', recipe)
                self.assertIn('KC_PUSH_DIR=', recipe)
                self.assertIn('KC_PROVIDER_KEYS_FILE=', recipe)
                self.assertIn('mktemp -d', recipe)


if __name__ == '__main__':
    unittest.main()
