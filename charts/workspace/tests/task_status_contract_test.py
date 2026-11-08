"""The Build status vocabulary, and that all three codebases share it.

task_status.py is the source of truth for the backend. The web and mobile apps
cannot import it, so their helpers repeat its groups; this test reads those
TypeScript files and fails if any of them drift. It lives in the Python suite
because that is the one runner that can see all three trees.

The groups are spelled out by hand here, so changing what a status means has to
change a test too. The parsing fails loudly: a helper that moves or changes
shape is a failure, never a skip.

Run:  python3 -m unittest tests.task_status_contract_test   (from charts/workspace/)
"""

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(HERE)
sys.path.insert(0, WORKSPACE)
import task_status  # noqa: E402

REPO = os.path.dirname(os.path.dirname(WORKSPACE))
WEB_HELPER = os.path.join(WORKSPACE, 'web', 'src', 'util', 'taskStatus.ts')
WEB_API = os.path.join(WORKSPACE, 'web', 'src', 'api', 'tasks.ts')
MOBILE_HELPER = os.path.join(REPO, 'mobile', 'src', 'util', 'taskStatus.ts')

RUNNING = 'running'
WAITING = 'waiting-for-input'
TERMINAL = {'completed', 'error', 'killed', 'terminated'}


def _source(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _match(pattern, path):
    found = re.search(pattern, _source(path), re.S)
    if found is None:
        raise AssertionError(f'{pattern!r} not found in {path}')
    return found.group(1)


def ts_list(path, name):
    """The strings of `export const NAME = [...]`."""
    return set(re.findall(r"'([^']+)'", _match(
        rf'export const {name} = \[(.*?)\]', path)))


class BackendVocabularyTests(unittest.TestCase):
    def test_wire_strings_and_their_order(self):
        self.assertEqual(
            task_status.ALL_STATUSES,
            (RUNNING, WAITING, 'completed', 'error', 'killed', 'terminated'))

    def test_groups_mean_what_they_say(self):
        self.assertEqual(task_status.LIVE_STATUSES, {RUNNING, WAITING})
        self.assertEqual(task_status.TERMINAL_STATUSES, TERMINAL)
        self.assertEqual(task_status.FAILED_STATUSES, TERMINAL - {'completed'})

    def test_waiting_is_quiescent_but_not_terminal(self):
        # The two sets differ on purpose. A watcher or a board run treats a
        # parked Build as done; the transition table and the worktree sweep
        # must not, or parking would end the Build and lose its worktree.
        self.assertEqual(task_status.QUIESCENT_STATUSES, TERMINAL | {WAITING})
        self.assertNotIn(WAITING, task_status.TERMINAL_STATUSES)

    def test_a_terminal_status_can_never_move(self):
        for status in TERMINAL:
            for target in task_status.ALL_STATUSES:
                self.assertFalse(task_status.can_transition(status, target),
                                 (status, target))

    def test_every_status_has_a_mission_state(self):
        self.assertEqual(set(task_status.MISSION_STATE),
                         set(task_status.ALL_STATUSES))


class ClientVocabularyTests(unittest.TestCase):
    """Web and mobile must name exactly the backend's groups."""

    def test_web_groups(self):
        self.assertEqual(ts_list(WEB_HELPER, 'ALIVE_STATUSES'),
                         set(task_status.LIVE_STATUSES))
        self.assertEqual(ts_list(WEB_HELPER, 'FINISHED_STATUSES'),
                         set(task_status.TERMINAL_STATUSES))
        self.assertEqual(ts_list(WEB_HELPER, 'FAILED_STATUSES'),
                         set(task_status.FAILED_STATUSES))

    def test_web_task_status_type_is_the_backend_vocabulary(self):
        union = _match(r'export type TaskStatus =(.*?);', WEB_API)
        # `unknown` is the client's own value for a status it cannot read.
        self.assertEqual(set(re.findall(r"'([^']+)'", union)) - {'unknown'},
                         set(task_status.ALL_STATUSES))

    def test_mobile_groups_through_its_aliases(self):
        body = _match(r'export const WIRE_STATUS_ALIASES = \{(.*?)\}',
                      MOBILE_HELPER)
        aliases = dict(re.findall(r"'?([\w-]+)'?\s*:\s*'([^']+)'", body))
        self.assertEqual(aliases, {WAITING: 'waiting', 'completed': 'done'})

        def mobile(statuses):
            return {aliases.get(s, s) for s in statuses}

        self.assertEqual(ts_list(MOBILE_HELPER, 'ACTIVE_STATUSES'),
                         mobile(task_status.LIVE_STATUSES))
        self.assertEqual(ts_list(MOBILE_HELPER, 'FINISHED_STATUSES'),
                         mobile(task_status.TERMINAL_STATUSES))


if __name__ == '__main__':
    unittest.main()
