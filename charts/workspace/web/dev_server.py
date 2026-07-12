"""Local-only test harness that runs server.py's BrowserHandler on a chosen
port with DASHBOARD_DIST_DIR pointed at this project's freshly-built dist/.

Used to take Playwright screenshots of the SPA without disturbing the real
workspace dashboard on port 6080.

Run:
    python3 charts/workspace/web/dev_server.py [port]

Default port: 7070.
"""
import http.server
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.path.dirname(HERE)
DIST = os.path.join(HERE, 'dist')

sys.path.insert(0, WORKSPACE_DIR)
import server  # noqa: E402

# Dev relaxation: skip auth so the SPA can hit /api/* without the OAuth proxy
# headers or a bearer token. Production server.py is unchanged.
# Accept any args/kwargs — server.py's real signatures have grown extra
# keyword args over time (e.g. check_claude_auth(allow_none_mode=...)); a
# tolerant stub keeps this harness working as those evolve.
server.BrowserHandler.check_claude_auth = lambda self, *a, **k: True
server.BrowserHandler.check_oauth_only = lambda self, *a, **k: True

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7070

os.environ['DASHBOARD_DIST_DIR'] = DIST
os.chdir(WORKSPACE_DIR)

print(f'[dev_server] DASHBOARD_DIST_DIR={DIST}')
print(f'[dev_server] cwd={os.getcwd()}')
print(f'[dev_server] listening on http://127.0.0.1:{PORT}')
print(f'[dev_server] dashboard SPA: http://127.0.0.1:{PORT}/')

httpd = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), server.BrowserHandler)
try:
    httpd.serve_forever()
except KeyboardInterrupt:
    httpd.shutdown()
