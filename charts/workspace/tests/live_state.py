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

The Makefile's python-tests target is the second line of defence: it points
KC_FEED_DIR / KC_PUSH_DIR at a throwaway directory for the whole run, so a test
that forgets this helper still cannot reach the live Feed or a phone.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
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
