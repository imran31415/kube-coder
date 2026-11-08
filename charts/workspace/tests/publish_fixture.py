"""Loopback-only E2E fixture: real publisher/Git/bare remote, fake GitHub/model.

Run on Linux: python3 -m tests.publish_fixture 7108
Never imported by or exposed through the production server.
"""
import http.server
import json
import os
from pathlib import Path
import sys

from tests.build_publish_test import PublishFixture
from tests.git_fixtures import git, write
import publish_git
import server

fixture = PublishFixture()
fixture.setUp()
fixture.service.background = True
server._PUBLISHER = fixture.service
server.READONLY_MODE = False
server.BrowserHandler.check_claude_auth = lambda *a, **kw: True


class Handler(server.BrowserHandler):
    def do_GET(self):
        if self.path == '/fixture/assertions':
            status = fixture.service.status('t1')
            self.send_json({'status': status, 'pr_creates': fixture.github.creates,
                'remote_sha': publish_git.remote_sha(fixture.repo, fixture.remote, fixture.dest['head_branch']),
                'head_sha': git(fixture.path, 'rev-parse', 'HEAD'),
                'commit_count': int(git(fixture.path, 'rev-list', '--count', 'main..HEAD')),
                'main_clean': git(fixture.repo, 'status', '--porcelain') == ''})
            return
        super().do_GET()

    def do_POST(self):
        if self.path == '/fixture/change':
            write(os.path.join(fixture.path, 'f0.txt'), 'A second reviewed update\n')
            self.send_json({'ok': True})
            return
        super().do_POST()


try:
    with http.server.ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1]) if len(sys.argv) > 1 else 7108), Handler) as httpd:
        print('Publishing fixture ready', flush=True)
        httpd.serve_forever()
finally:
    fixture.doCleanups()
