"""Keep a test off the workspace's live Feed and away from real phones (#685).

WHY THIS EXISTS. `FeedManager.emit()` appends to the Feed log and hands every
item to `push_notify.dispatch()`, which POSTs to Expo for each phone registered
in that workspace. A test that reaches emit() — directly, or through
`_reconcile_status`, a trigger, a board review or a decision memory — with the
default paths writes `/home/dev/.claude-feed` and pages whoever registered a
phone there. `WaitingQuiescenceTests` did exactly that for two months: 193
"Task waiting on you: tq-1" alerts, 41% of every push in the maintainer's log,
one per kc-preflight run. CI never noticed, because a runner has no phone.

Redirecting only the feed files is not enough — `feed_test.py` did that and
still pushed for real, because the token store kept its live path. So this
moves both, and by default replaces the network call with a recorder:

    sys.path.insert(0, HERE)          # HERE = this tests/ directory
    from live_state import isolate_feed_and_push

    def setUp(self):
        isolate_feed_and_push(self)   # self.expo_calls records any push

which works under both `python3 -m unittest discover -s tests` (the Makefile)
and `python3 -m unittest tests.<name>` (the per-file docstrings). Everything is
undone through addCleanup, so it composes with any tearDown.

`isolate_provider_keys` is the same idea for the OTHER live store a test can
reach: the API keys the user set in Settings. `available_assistants()` gates
the assistant list on them, so on a workspace whose owner has an OpenRouter key
`AssistantSelectionTests` read that key and failed two assertions that pass in
CI, which has no such file. The write direction is the worse one — a test that
calls `ProviderKeysManager.set` without redirecting `KEYS_FILE` overwrites real
API keys.

`isolate_trigger_runs` is the same idea for the THIRD live store: the per-trigger
run ledger (#91). Every webhook fire, cron fire and page-watch check now appends
one entry, so a test that exercises those handlers writes the workspace owner's
real trigger history — and once a suite starts asserting on the ledger, entries
left behind by an earlier test are also a source of cross-test flake.

The Makefile's python-tests target is the second line of defence: it points
KC_FEED_DIR / KC_PUSH_DIR / KC_PROVIDER_KEYS_FILE / KC_TRIGGER_RUNS_DIR at a
throwaway directory for the whole run, so a test that forgets these helpers
still cannot reach the live Feed, a phone, the user's keys, or their trigger
history.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import hypervisor_session  # noqa: E402
import push_notify  # noqa: E402
import server  # noqa: E402


def isolate_feed_and_push(tc, stub_expo=True):
    """Point the Feed and the push token/ledger files at a fresh temp dir for
    the lifetime of test case `tc`. With `stub_expo`, `push_notify._post_expo`
    records each batch in `tc.expo_calls` and answers `ok` instead of reaching
    exp.host. Returns the temp root."""
    root = tempfile.mkdtemp(prefix='kctest-live-')
    tc.addCleanup(shutil.rmtree, root, True)
    feed_dir = os.path.join(root, 'feed')
    push_dir = os.path.join(root, 'push')
    fm = server.FeedManager
    for target, attr, value in (
        (fm, 'FEED_DIR', feed_dir),
        (fm, 'ITEMS_PATH', os.path.join(feed_dir, 'items.jsonl')),
        (fm, 'STATE_PATH', os.path.join(feed_dir, 'state.json')),
        (push_notify, 'PUSH_DIR', push_dir),
        (push_notify, 'TOKENS_PATH', os.path.join(push_dir, 'tokens.json')),
        (push_notify, 'SENT_PATH', os.path.join(push_dir, 'sent.json')),
    ):
        patcher = mock.patch.object(target, attr, value)
        patcher.start()
        tc.addCleanup(patcher.stop)

    tc.expo_calls = []
    if stub_expo:
        def record(messages):
            tc.expo_calls.append(messages)
            return {'data': [{'status': 'ok'} for _ in messages]}
        patcher = mock.patch.object(push_notify, '_post_expo', side_effect=record)
        patcher.start()
        tc.addCleanup(patcher.stop)
    return root


def isolate_provider_keys(tc, keys=None):
    """Point the self-service provider-key store at a fresh temp file for the
    lifetime of test case `tc`, seeded with `keys` (default: none at all).

    Both readers are moved, because they read the same file independently:
    `server.ProviderKeysManager.KEYS_FILE` and
    `hypervisor_session._PROVIDER_KEYS_FILE`. Returns the path.

    Default-empty is the point. A test asserting "this assistant is disabled
    when its key is unset" is asserting something about env vars, and it must
    not quietly become a statement about whoever is running the suite.
    """
    root = tempfile.mkdtemp(prefix='kctest-keys-')
    tc.addCleanup(shutil.rmtree, root, True)
    path = os.path.join(root, 'provider-keys.json')
    if keys:
        with open(path, 'w') as f:
            json.dump(dict(keys), f)
    for target, attr in ((server.ProviderKeysManager, 'KEYS_FILE'),
                         (hypervisor_session, '_PROVIDER_KEYS_FILE')):
        patcher = mock.patch.object(target, attr, path)
        patcher.start()
        tc.addCleanup(patcher.stop)
    return path


def isolate_trigger_runs(tc):
    """Point the per-trigger run ledger at a fresh temp dir for the lifetime of
    test case `tc`, and return that dir (#91).

    Any test that reaches a webhook/cron/page-watch fire handler needs this:
    those handlers append one ledger entry per call, and with the default path
    that is the workspace owner's own trigger history. It is also what makes
    ledger assertions deterministic — `list_runs` reads whatever is on disk, so
    a previous test's entries would count toward this one's totals.
    """
    root = tempfile.mkdtemp(prefix='kctest-trigruns-')
    tc.addCleanup(shutil.rmtree, root, True)
    patcher = mock.patch.object(server.TriggerRunsManager, 'RUNS_DIR', root)
    patcher.start()
    tc.addCleanup(patcher.stop)
    return root


def silence_prompt_delivery(tc):
    """Stop `create_task` from leaving a live prompt-delivery thread behind.

    `ClaudeTaskManager.create_task` spawns a daemon thread that waits for the
    new tmux pane to be ready and then pastes the prompt. In a test the pane
    never exists — but the thread outlives the test, and by the time it runs,
    the test's `mock.patch('server.subprocess.run')` has been undone. So it
    shells out to the REAL tmux, against the developer's own tmux server, once
    or twice a second for as long as its ceiling allows.

    That is a live-state leak like any other here, and it also poisons whoever
    patches `subprocess.run` next: an unrelated test asserting "my endpoint ran
    this argv" can capture a stray `tmux capture-pane` from a test that already
    finished. `server.py` now gives up as soon as the session reads as gone,
    which bounds it; this removes it outright for tests that do not care about
    prompt delivery at all.
    """
    for attr, value in (('_wait_for_pane_ready', False),
                        ('_deliver_prompt', True)):
        patcher = mock.patch.object(server.ClaudeTaskManager, attr,
                                    return_value=value)
        patcher.start()
        tc.addCleanup(patcher.stop)
