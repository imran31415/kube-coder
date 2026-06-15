"""Unit tests for server.py's DesktopManager and WorkspaceManager.

DesktopManager: launcher-grid CRUD + per-action-type validation, backed by
a single JSON file (atomic write, first-load seeding).
WorkspaceManager: lists candidate project dirs under /home/dev.

CONFIG_PATH / HOME_DIR are redirected to tempdirs. No HTTP handler needed.

Run with:    python3 -m unittest tests.server_desktop_workspace_test
(from charts/workspace/)
"""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402

DT = server.DesktopManager
WS = server.WorkspaceManager


# ───────────────────────── DesktopManager ─────────────────────────

class DesktopBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_path = DT.CONFIG_PATH
        self._orig_dir = DT.CONFIG_DIR
        DT.CONFIG_DIR = self.tmp
        DT.CONFIG_PATH = os.path.join(self.tmp, 'desktop.json')
        self.addCleanup(self._restore)

    def _restore(self):
        DT.CONFIG_PATH = self._orig_path
        DT.CONFIG_DIR = self._orig_dir

    def _empty_config(self):
        with open(DT.CONFIG_PATH, 'w') as f:
            json.dump({'version': 1, 'items': []}, f)

    def _task_item(self, **over):
        item = {'label': 'Build', 'icon': '📝',
                'action': {'type': 'task', 'prompt': 'hi', 'workdir': '/home/dev'}}
        item.update(over)
        return item


class LoadSeedTests(DesktopBase):
    def test_first_load_seeds_defaults(self):
        data = DT._load_all()
        self.assertEqual(data['version'], 1)
        self.assertEqual(len(data['items']), len(DT._SEED_ITEMS))
        self.assertTrue(os.path.exists(DT.CONFIG_PATH))  # seed persisted

    def test_corrupt_file_returns_empty(self):
        with open(DT.CONFIG_PATH, 'w') as f:
            f.write('{bad json')
        self.assertEqual(DT._load_all()['items'], [])

    def test_non_dict_returns_empty(self):
        with open(DT.CONFIG_PATH, 'w') as f:
            json.dump([1, 2], f)
        self.assertEqual(DT._load_all()['items'], [])


class ValidateTests(DesktopBase):
    def test_non_dict(self):
        with self.assertRaises(ValueError):
            DT._validate('nope')

    def test_label_bounds(self):
        with self.assertRaises(ValueError):
            DT._validate(self._task_item(label=''))
        with self.assertRaises(ValueError):
            DT._validate(self._task_item(label='x' * 81))

    def test_icon_bounds(self):
        with self.assertRaises(ValueError):
            DT._validate(self._task_item(icon=''))
        with self.assertRaises(ValueError):
            DT._validate(self._task_item(icon='x' * 33))

    def test_hotkey_validation(self):
        ok = DT._validate(self._task_item(hotkey='cmd+shift+1'))
        self.assertEqual(ok['hotkey'], 'cmd+shift+1')
        with self.assertRaises(ValueError):
            DT._validate(self._task_item(hotkey='bad/key!'))
        # blank hotkey drops out
        self.assertNotIn('hotkey', DT._validate(self._task_item(hotkey='  ')))

    def test_action_must_be_object_and_known_type(self):
        with self.assertRaises(ValueError):
            DT._validate({'label': 'a', 'icon': 'x', 'action': 'no'})
        with self.assertRaises(ValueError):
            DT._validate({'label': 'a', 'icon': 'x', 'action': {'type': 'bogus'}})

    def test_task_action_defaults_workdir_and_caps_prompt(self):
        cleaned = DT._validate(self._task_item(action={'type': 'task', 'prompt': 'p'}))
        self.assertEqual(cleaned['action']['workdir'], '/home/dev')
        with self.assertRaises(ValueError):
            DT._validate(self._task_item(action={'type': 'task', 'prompt': 'x' * 8001}))

    def test_url_action(self):
        ok = DT._validate({'label': 'L', 'icon': 'x',
                           'action': {'type': 'url', 'url': '/memory', 'target': 'self'}})
        self.assertEqual(ok['action']['target'], 'self')
        with self.assertRaises(ValueError):
            DT._validate({'label': 'L', 'icon': 'x', 'action': {'type': 'url', 'url': 'ftp://x'}})
        with self.assertRaises(ValueError):
            DT._validate({'label': 'L', 'icon': 'x',
                          'action': {'type': 'url', 'url': '/ok', 'target': 'popup'}})

    def test_shell_action(self):
        ok = DT._validate({'label': 'L', 'icon': 'x',
                           'action': {'type': 'shell', 'command': 'ls', 'timeout': 10}})
        self.assertEqual(ok['action']['timeout'], 10)
        with self.assertRaises(ValueError):
            DT._validate({'label': 'L', 'icon': 'x', 'action': {'type': 'shell', 'command': ''}})
        with self.assertRaises(ValueError):
            DT._validate({'label': 'L', 'icon': 'x',
                          'action': {'type': 'shell', 'command': 'ls', 'timeout': 99999}})


class CrudTests(DesktopBase):
    def setUp(self):
        super().setUp()
        self._empty_config()

    def test_create_assigns_id(self):
        created = DT.create(self._task_item())
        self.assertRegex(created['id'], r'^[a-f0-9]{8}$')
        self.assertEqual(DT.list_items()[0]['label'], 'Build')

    def test_get_found_and_missing(self):
        created = DT.create(self._task_item())
        self.assertEqual(DT.get(created['id'])['id'], created['id'])
        self.assertIsNone(DT.get('nonexistent'))

    def test_update(self):
        created = DT.create(self._task_item())
        updated = DT.update(created['id'], self._task_item(label='Renamed'))
        self.assertEqual(updated['label'], 'Renamed')
        self.assertEqual(updated['id'], created['id'])

    def test_update_invalid_id_and_missing(self):
        with self.assertRaises(ValueError):
            DT.update('!!', self._task_item())
        with self.assertRaises(ValueError):
            DT.update('abcd1234', self._task_item())  # well-formed but absent

    def test_delete(self):
        created = DT.create(self._task_item())
        DT.delete(created['id'])
        self.assertEqual(DT.list_items(), [])
        with self.assertRaises(ValueError):
            DT.delete(created['id'])  # already gone

    def test_reorder(self):
        a = DT.create(self._task_item(label='A'))
        b = DT.create(self._task_item(label='B'))
        c = DT.create(self._task_item(label='C'))
        out = DT.reorder([c['id'], a['id']])  # b omitted -> appended
        labels = [it['label'] for it in out]
        self.assertEqual(labels[:2], ['C', 'A'])
        self.assertIn('B', labels)
        with self.assertRaises(ValueError):
            DT.reorder('not-a-list')


# ───────────────────────── WorkspaceManager ─────────────────────────

class WorkspaceManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = WS.HOME_DIR
        WS.HOME_DIR = self.tmp
        self.addCleanup(lambda: setattr(WS, 'HOME_DIR', self._orig))

    def _mkdir(self, name):
        os.makedirs(os.path.join(self.tmp, name), exist_ok=True)
        return os.path.join(self.tmp, name)

    def test_lists_projects_and_flags(self):
        proj = self._mkdir('myproj')
        open(os.path.join(proj, 'package.json'), 'w').close()
        git = self._mkdir('repo')
        os.makedirs(os.path.join(git, '.git'))
        self._mkdir('plain')
        out = WS.list_dirs()
        by_label = {d['label']: d for d in out}
        self.assertTrue(by_label['myproj']['is_project'])
        self.assertFalse(by_label['myproj']['is_git_repo'])
        self.assertTrue(by_label['repo']['is_git_repo'])
        self.assertTrue(by_label['repo']['is_project'])
        self.assertFalse(by_label['plain']['is_project'])

    def test_skips_hidden_and_skipnames_and_files(self):
        self._mkdir('.hidden')
        self._mkdir('node_modules')
        open(os.path.join(self.tmp, 'afile'), 'w').close()
        self._mkdir('keep')
        labels = {d['label'] for d in WS.list_dirs()}
        self.assertEqual(labels, {'keep'})

    def test_missing_home_returns_empty(self):
        WS.HOME_DIR = '/no/such/home/dir'
        self.assertEqual(WS.list_dirs(), [])

    def test_sorted_by_mtime_desc(self):
        old = self._mkdir('old')
        new = self._mkdir('new')
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        out = WS.list_dirs()
        self.assertEqual(out[0]['label'], 'new')


if __name__ == '__main__':
    unittest.main()
