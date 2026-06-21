"""Unit tests for parent_task_id lineage on the HTTP create path (issue #111).

create_task accepts parent_task_id and records it on the child, but until now
nothing appended the child to the *parent's* sub_task_ids — so API-created
sub-agent lineage was always empty. These cover the append behaviour.

tmux is stubbed; TASKS_DIR is a tempdir.

Run with:    python3 -m unittest tests.task_lineage_test
(from charts/workspace/)
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402

CTM = server.ClaudeTaskManager


def _tmux_ok(*args, **kwargs):
    return mock.Mock(returncode=0, stdout='', stderr='')


class LineageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kclin-')
        self._orig = CTM.TASKS_DIR
        CTM.TASKS_DIR = self.tmp
        self.addCleanup(self._restore)

    def _restore(self):
        CTM.TASKS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _meta(self, task_id):
        with open(os.path.join(self.tmp, task_id, 'task.json')) as f:
            return json.load(f)

    @mock.patch('server.subprocess.run', side_effect=_tmux_ok)
    def test_child_records_parent_and_parent_lists_child(self, _run):
        parent = CTM.create_task('parent work')
        child = CTM.create_task('child work', parent_task_id=parent['task_id'])
        # Child carries its parent.
        self.assertEqual(child['parent_task_id'], parent['task_id'])
        # Parent's persisted meta now lists the child.
        self.assertEqual(self._meta(parent['task_id'])['sub_task_ids'],
                         [child['task_id']])

    @mock.patch('server.subprocess.run', side_effect=_tmux_ok)
    def test_multiple_children_append_and_dedupe(self, _run):
        parent = CTM.create_task('p')
        c1 = CTM.create_task('c1', parent_task_id=parent['task_id'])
        c2 = CTM.create_task('c2', parent_task_id=parent['task_id'])
        subs = self._meta(parent['task_id'])['sub_task_ids']
        self.assertEqual(subs, [c1['task_id'], c2['task_id']])
        # Idempotent append helper — re-appending an existing child is a no-op.
        CTM._append_sub_task_id(parent['task_id'], c1['task_id'])
        self.assertEqual(self._meta(parent['task_id'])['sub_task_ids'], subs)

    @mock.patch('server.subprocess.run', side_effect=_tmux_ok)
    def test_no_parent_is_noop(self, _run):
        task = CTM.create_task('solo')
        self.assertIsNone(task['parent_task_id'])
        self.assertEqual(task['sub_task_ids'], [])

    def test_append_missing_parent_is_safe(self):
        # Must not raise when the parent task doesn't exist.
        CTM._append_sub_task_id('ghost-parent', 'child-1')
        CTM._append_sub_task_id('', 'child-1')  # empty parent → no-op

    @mock.patch('server.subprocess.run', side_effect=_tmux_ok)
    def test_list_tasks_filter_by_parent(self, _run):
        parent = CTM.create_task('p')
        child = CTM.create_task('c', parent_task_id=parent['task_id'])
        CTM.create_task('unrelated')
        kids = CTM.list_tasks(parent=parent['task_id'])
        self.assertEqual([t['task_id'] for t in kids], [child['task_id']])


if __name__ == '__main__':
    unittest.main()
