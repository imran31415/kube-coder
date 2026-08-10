"""Local-only test harness that runs server.py's BrowserHandler on a chosen
port with DASHBOARD_DIST_DIR pointed at this project's freshly-built dist/.

Used to take Playwright screenshots of the SPA without disturbing the real
workspace dashboard on port 6080, and — since #588 — to drive the Board
Processor against REAL external boards from a dev machine.

Run:
    python3 charts/workspace/web/dev_server.py [port]

Default port: 7070.

`.env.local` (gitignored; see .env.local.example) is loaded if present, and
board state is redirected to a local scratch dir so nothing writes to
/home/dev. Both are dev-only: server.py itself is unchanged.
"""
import http.server
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.path.dirname(HERE)
DIST = os.path.join(HERE, 'dist')


def _load_env_local():
    """Read charts/workspace/.env.local into os.environ, if it exists.

    Deliberately a hand-rolled 6-line parser rather than python-dotenv: this
    repo's workspace code is stdlib-only, and a dev harness is not the place
    to introduce the first dependency. Existing env wins, so
    `KC_MAX_TASKS=2 python3 dev_server.py` still overrides the file.
    """
    path = os.path.join(WORKSPACE_DIR, '.env.local')
    if not os.path.isfile(path):
        return False
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value
    return True


_HAS_ENV_LOCAL = _load_env_local()

sys.path.insert(0, WORKSPACE_DIR)
import server  # noqa: E402

# Board state on the PVC lives under /home/dev; on a dev box that is either
# absent or someone's real home. Redirect it to a scratch dir the repo ignores.
_LOCAL_HOME = os.environ.get('KC_LOCAL_HOME')
if _LOCAL_HOME:
    _LOCAL_HOME = os.path.abspath(os.path.join(WORKSPACE_DIR, _LOCAL_HOME))
    os.makedirs(_LOCAL_HOME, exist_ok=True)
    for _mgr in ('BoardsManager', 'BoardCredentialsManager'):
        _cls = getattr(server, _mgr, None)
        if _cls is not None:
            _cls.HOME_ROOT = _LOCAL_HOME
    print(f'[dev_server] board state -> {_LOCAL_HOME}')

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

if _HAS_ENV_LOCAL:
    print('[dev_server] loaded .env.local')
    # Seed the board credential store from the env file so a real GitHub /
    # Jira board authenticates without anyone pasting a token into the UI.
    # Values go into the credential store and nowhere else — in particular
    # never into an env overlay handed to an agent subprocess.
    _bcm = getattr(server, 'BoardCredentialsManager', None)
    if _bcm is not None:
        _gh = os.environ.get('KC_BOARD_GITHUB_TOKEN')
        if _gh:
            _bcm.set('GITHUB_BOARD_TOKEN', _gh)
            print('[dev_server] board credential @board-creds/GITHUB_BOARD_TOKEN')
        _jira = os.environ.get('KC_BOARD_JIRA_TOKEN')
        _email = os.environ.get('KC_BOARD_JIRA_EMAIL')
        if _jira and _email:
            ok, err = _bcm.set('JIRA_API_TOKEN', _jira, fmt='basic',
                               username=_email)
            print('[dev_server] board credential @board-creds/JIRA_API_TOKEN'
                  if ok else f'[dev_server] Jira credential rejected: {err}')
        _zd = os.environ.get('KC_BOARD_ZENDESK_TOKEN')
        if _zd:
            # An OAuth ACCESS TOKEN, sent as a bearer — not an API token with a
            # `/token` username. Zendesk withdrew API-token creation from the
            # admin UI and retires the existing ones on 2027-04-30, so the
            # Basic scheme every integration guide still shows is unobtainable
            # for a new account.
            ok, err = _bcm.set('ZENDESK_OAUTH_TOKEN', _zd, fmt='token')
            print('[dev_server] board credential @board-creds/ZENDESK_OAUTH_TOKEN'
                  if ok else f'[dev_server] Zendesk credential rejected: {err}')

# The boot sweep. server.py runs this from its `__main__` block, which this
# harness bypasses by importing server as a module — so without it a run left
# in flight by a previous process stayed `running` forever and its leases
# pinned their items, making every later run report "another run holds this
# item". Restarting the dev server has to mean the same thing here as it does
# in the pod.
if getattr(server, '_BOARDS_AVAILABLE', False):
    try:
        server.BoardRunsManager.start()
    except Exception as _e:                                  # noqa: BLE001
        print(f'[dev_server] board boot sweep failed: {_e}', file=sys.stderr)

# Loopback by default: this process holds REAL board tokens, and a dev harness
# that silently listened on every interface would put them one stray `curl`
# from anyone on the same wifi. Set KC_DEV_BIND=0.0.0.0 to run inside a
# container, where the isolation comes from Docker's port mapping instead.
BIND = os.environ.get('KC_DEV_BIND') or '127.0.0.1'

print(f'[dev_server] DASHBOARD_DIST_DIR={DIST}')
print(f'[dev_server] cwd={os.getcwd()}')
print(f'[dev_server] listening on http://{BIND}:{PORT}')
print(f'[dev_server] dashboard SPA: http://127.0.0.1:{PORT}/')

httpd = http.server.ThreadingHTTPServer((BIND, PORT), server.BrowserHandler)
try:
    httpd.serve_forever()
except KeyboardInterrupt:
    httpd.shutdown()
