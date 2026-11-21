"""Loopback-only real HTTP/UI workflow with in-memory external dependencies.

Serves the real signup listener on :8089 and, sharing the same state, the real
admin console Handler on :8088 (for the signup-access page)."""
import http.server
import json
import os
import pathlib
import sys
import subprocess
import tempfile
import textwrap
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
os.environ['SIGNUP_ORIGIN'] = 'http://127.0.0.1:8089'
from signup_test import fixture
from signup_allowlist_test import github_lookup
from signup import handler, signup_html
from signup_allowlist import Allowlist
from signup_errors import SignupError
import controller

service = fixture()
service.backend.lookup.side_effect = github_lookup
rendered = subprocess.run(['helm', 'template', 'test', str(ROOT), '-f', str(ROOT / 'tests/test-values.yaml'),
    '--show-only', 'templates/controller-web-configmap.yaml'], check=True, capture_output=True,
    text=True, encoding='utf-8').stdout
console_html = textwrap.dedent(rendered.split('  index.html: |\n', 1)[1]).encode()
html = signup_html(console_html)

# The admin console: same Handler as production, proxy-header admin identity.
# Only the fleet list (kubectl) is stubbed; the signup-access page never uses it.
dist = tempfile.mkdtemp(prefix='kc-console-fixture-')
pathlib.Path(dist, 'index.html').write_bytes(console_html)
controller.DIST_DIR, controller.SIGNUP_SERVICE, controller.SIGNUP_ENABLED = dist, service, True
controller.cached_list_workspaces = lambda force=False: []
controller.decorate_with_updates = lambda _: {'namespace': 'fixture', 'workspaces': [], 'latestVersion': None}
admin = http.server.ThreadingHTTPServer(('127.0.0.1', 8088), controller.Handler)
threading.Thread(target=admin.serve_forever, daemon=True).start()
class FixtureHandler(handler(service, 'http://127.0.0.1:8089', 'fixture-only-csrf-secret', html)):
    # Fixture controls exist only in this loopback test process, never the app.
    def do_POST(self):
        if self.path == '/__fixture/reset':
            mode = json.loads(self.rfile.read(int(self.headers['Content-Length'])))['mode']
            with service.store.lock:
                fresh = fixture()
                service.store.records.clear()
                service.store.secrets.clear()
                service.backend, service.dex = fresh.backend, fresh.dex
                service.backend.lookup.side_effect = github_lookup
                service.allowlist = Allowlist(service.store, {'123'})   # drop the cached list too
                if mode == 'dex_failure':
                    service.dex.ensure.side_effect = [SignupError('dex_unavailable'), None]
                elif mode == 'owner_conflict':
                    service.backend.occupied.return_value = True
            self.send_json({'ok': True})
        elif self.path == '/__fixture/delete':
            self.send_json(service.cleanup('octocat'))
        else:
            super().do_POST()

server = http.server.ThreadingHTTPServer(('127.0.0.1', 8089), FixtureHandler)

def work():
    while True:
        service.tick()
        time.sleep(0.5)

threading.Thread(target=work, daemon=True).start()
print('Signup fixture listening on 127.0.0.1:8089, admin console on 127.0.0.1:8088', flush=True)
server.serve_forever()
