"""Loopback-only real HTTP/UI workflow with in-memory external dependencies."""
import http.server
import json
import pathlib
import sys
import subprocess
import textwrap
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
from signup_test import fixture
from signup import handler, signup_html
from signup_errors import SignupError

service = fixture()
rendered = subprocess.run(['helm', 'template', 'test', str(ROOT), '-f', str(ROOT / 'tests/test-values.yaml'),
    '--show-only', 'templates/controller-web-configmap.yaml'], check=True, capture_output=True,
    text=True, encoding='utf-8').stdout
html = textwrap.dedent(rendered.split('  index.html: |\n', 1)[1]).encode()
html = signup_html(html)
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
print('Signup fixture listening on 127.0.0.1:8089', flush=True)
server.serve_forever()
