"""Applications: the locally-listening apps surface (#100).

Six routes on three verbs, plus the app-session cookie mint that the proxy
depends on.

What is NOT here is the proxy itself. `_dispatch_app_proxy`,
`_proxy_app_request` and `_proxy_app_websocket` stay in server.py because
they are **middleware, not routes**: the app proxy runs as a stage inside
each verb's dispatch, before or after the tables depending on the verb, and
it matches a `/api/app-proxy/<port>/…` prefix rather than a registered path.
Issue #100's end state is "server.py reduced to bootstrap + middleware", and
the proxy is the middleware.

`handle_app_session_mint` does live here, though, because it IS a route
(`GET /api/claude/apps/session`) even though what it mints is the cookie the
proxy later reads.

The three browser launchers (`/api/launch-chrome`, `/api/open-localhost`,
`/api/test-chrome`) come along as the oldest members of the same surface —
they are how the workspace opened a local app before the proxy existed.
`/api/launch-firefox` and `/api/test-firefox` are kept as aliases, as they
were: two more registrations pointing at the same two handlers.
"""

import json
import os
import re
import subprocess
import time
import urllib.parse

import handlers
from handlers.routing import RouteTable


class AppRoutes:
    """BrowserHandler methods backing `ROUTES`."""

    def _handle_apps_list(self):
        if not self.check_app_proxy_auth():
            self.send_response(401)
            self.end_headers()
            return
        # Embedded iframes need cookie-based auth; bearer-only deployments
        # can't auth iframe sub-resource requests. Let the SPA show a clear
        # explanation instead of the user staring at mysterious 401s.
        unavailable = None
        if handlers.server.AUTH_MODE != 'oauth2':
            unavailable = ('Applications requires the workspace to run behind an OAuth2 '
                           'proxy so iframe sub-resource requests can authenticate via cookies. '
                           'Current AUTH_MODE is "{}".'.format(handlers.server.AUTH_MODE))
        try:
            apps = handlers.server.AppsManager.list_apps()
        except Exception as e:
            self.send_json({'error': str(e)}, 500)
            return
        self.send_json({
            'apps': apps,
            'unavailable_reason': unavailable,
            'auth_mode': handlers.server.AUTH_MODE,
        })

    def _handle_apps_pin_create(self):
        if not self.check_claude_auth():
            self.send_response(401)
            self.end_headers()
            return
        try:
            body = self.read_json_body(max_bytes=4096) or {}
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        except json.JSONDecodeError:
            self.send_json({'error': 'invalid JSON'}, 400)
            return
        try:
            pin = handlers.server.AppsManager.add_pin(
                port=body.get('port'),
                name=body.get('name'),
                strip_prefix=bool(body.get('strip_prefix', False)),
            )
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        self.send_json({'ok': True, 'pin': {**pin, 'port': handlers.server.AppsManager._validate_port(body.get('port'))}}, 201)

    def _handle_apps_pin_delete(self, port):
        if not self.check_claude_auth():
            self.send_response(401)
            self.end_headers()
            return
        try:
            removed = handlers.server.AppsManager.remove_pin(port)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        self.send_json({'ok': True, 'removed': bool(removed)})

    def handle_app_session_mint(self):
        """GET /api/claude/apps/session?next=/api/app-proxy/<port>/

        Bearer-authenticated bootstrap for embedding an app in a native
        WebView: validates the caller, mints a short-lived app-session cookie
        (see ClaudeTaskManager.mint_app_session) and 302s to `next`. The
        WebView attaches its Authorization header to this one request, stores
        the Set-Cookie, follows the redirect, and every sub-resource the
        embedded app loads from then on authenticates via the cookie."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        qs = urllib.parse.urlsplit(self.path).query
        next_path = (urllib.parse.parse_qs(qs).get('next') or [''])[0]
        if not self._APP_SESSION_NEXT_RE.match(next_path):
            self.send_json({'error': 'next must be an /api/app-proxy/<port>/ path'}, 400)
            return
        value = handlers.server.ClaudeTaskManager.mint_app_session()
        # Secure only when the edge says HTTPS — a hard Secure flag would break
        # local http (kubectl port-forward) development.
        secure = '; Secure' if self.headers.get('X-Forwarded-Proto', '') == 'https' else ''
        cookie = (f'{self.APP_SESSION_COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; '
                  f'Max-Age={handlers.server.ClaudeTaskManager.APP_SESSION_TTL_SECONDS}{secure}')
        self.send_response(302)
        self.send_header('Set-Cookie', cookie)
        self.send_header('Location', next_path)
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def test_chrome(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            # Test browser installation
            browser_paths = [
                '/usr/local/bin/browser',
                '/usr/bin/lynx',
                '/usr/bin/w3m', 
                '/usr/bin/firefox-esr',
                '/usr/bin/firefox',
                '/usr/bin/chromium-browser',
                '/usr/bin/google-chrome'
            ]
            
            browser_path = None
            for path in browser_paths:
                if os.path.exists(path):
                    browser_path = path
                    break
            
            if not browser_path:
                self.send_error_response('Browser not found. Installation may have failed.')
                return
            
            # Test Xvfb display
            display = os.environ.get('DISPLAY', ':99')
            try:
                result = subprocess.run(['xdpyinfo', '-display', display], 
                                       capture_output=True, text=True, timeout=5)
                if result.returncode != 0:
                    # xdpyinfo failed, but check if Xvfb process is running instead.
                    # Match the display too: since #716 there are two Xvfbs
                    # (:99 for the user, :98 for agents), so a bare `pgrep Xvfb`
                    # would report the human's display healthy on the strength
                    # of the agent's.
                    if not handlers.server._xvfb_running(display):
                        self.send_error_response(f'X11 display {display} not available')
                        return
            except (subprocess.TimeoutExpired, FileNotFoundError):
                # xdpyinfo not available or timed out, check if Xvfb process is running
                if not handlers.server._xvfb_running(display):
                    self.send_error_response(f'X11 display {display} not available (Xvfb not running)')
                    return
            
            self.send_success_response(f'✅ Browser found at: {browser_path}\n✅ X11 display {display} available')
            
        except Exception as e:
            self.send_error_response(f'Test failed: {str(e)}')

    def launch_chrome(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            # Try different Chrome/Chromium locations
            browser_commands = [
                ('/usr/local/bin/browser', []),
                ('/usr/bin/firefox-esr', ['--safe-mode']),
                ('/usr/bin/firefox', ['--safe-mode']),
                ('firefox-esr', ['--safe-mode']),
                ('firefox', ['--safe-mode']),
                ('chromium-browser', ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']),
                ('/usr/bin/chromium-browser', ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']),
                ('/usr/bin/google-chrome', ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'])
            ]
            
            browser_cmd = None
            browser_args = []
            for cmd, args in browser_commands:
                if os.path.exists(cmd) or subprocess.run(['which', cmd], capture_output=True).returncode == 0:
                    browser_cmd = cmd
                    browser_args = args
                    break
            
            if not browser_cmd:
                self.send_error_response('No Chrome browser found. Download may have failed.')
                return
            
            env = os.environ.copy()
            env['DISPLAY'] = ':99'
            
            # Launch browser in background
            cmd_list = [browser_cmd] + browser_args + ['--new-window']
            process = subprocess.Popen(
                cmd_list, 
                env=env,
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )
            
            # Give it a moment to start
            time.sleep(2)
            
            if process.poll() is None:  # Process is still running
                self.send_success_response(f'✅ Chrome launched successfully (PID: {process.pid})')
            else:
                self.send_error_response('Chrome process exited immediately')
                
        except FileNotFoundError:
            self.send_error_response('Chrome not found. Please install Chrome first.')
        except Exception as e:
            self.send_error_response(f'Error launching Chrome: {str(e)}')

    def open_localhost(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            # Accept an optional {"port": <int>, "path": "<suffix>"} JSON
            # body so the dashboard's Preview pane can re-point the in-pod
            # browser without a code change. Path is appended after the
            # port (e.g. localhost:8080/admin or localhost:3000/?dev=1);
            # falls back to "/" when nothing is sent. The historical
            # port-only body still works.
            port = 8080
            url_path = '/'
            try:
                content_length = int(self.headers.get('Content-Length', 0) or 0)
                if content_length:
                    raw = self.rfile.read(content_length).decode('utf-8')
                    body = json.loads(raw) if raw else {}
                    if isinstance(body, dict):
                        if 'port' in body:
                            port = int(body['port'])
                        raw_path = str(body.get('path') or '').strip()
                        if raw_path:
                            # Normalize: ensure leading slash, no scheme,
                            # no host, no embedded newlines. Reject if it
                            # contains characters that don't belong in a
                            # path/query/fragment.
                            if '\n' in raw_path or '\r' in raw_path or ' ' in raw_path:
                                self.send_error_response('path must not contain whitespace or newlines')
                                return
                            if raw_path.lower().startswith(('http://', 'https://')):
                                self.send_error_response('path must be a relative suffix, not a full URL')
                                return
                            if not raw_path.startswith('/'):
                                raw_path = '/' + raw_path
                            url_path = raw_path
            except (ValueError, json.JSONDecodeError):
                self.send_error_response('Invalid JSON body — expected {"port": <int>, "path": "<suffix>"}')
                return
            if not (1 <= port <= 65535):
                self.send_error_response('port must be between 1 and 65535')
                return

            env = os.environ.copy()
            env['DISPLAY'] = ':99'

            url = f'http://localhost:{port}{url_path}'

            # Kill only browsers launched by this handler — pkill -f chrome
            # would also kill any user-spawned dev tool whose name includes
            # the substring (e.g. chrome-devtools-frontend). The marker dir
            # is unique to this handler and appears in every launched
            # browser's argv (chromium via --user-data-dir=, firefox via
            # -profile <dir>) so pkill -f on the literal path matches both.
            kc_user_data_dir = '/tmp/kc-managed-browser'
            os.makedirs(kc_user_data_dir, exist_ok=True)
            subprocess.run(['pkill', '-f', kc_user_data_dir],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            time.sleep(0.3)

            # --app and --start-fullscreen together give a kiosk-like surface:
            # no tabs, no URL bar, no window chrome — just the page. Combined
            # with vnc.html?resize=scale on the dashboard side, the Preview
            # pane ends up showing essentially only the browser content.
            # --user-data-dir is the marker pkill uses above to scope the
            # kill to only browsers we launched.
            chrome_args = [
                '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
                f'--user-data-dir={kc_user_data_dir}',
                '--start-fullscreen', f'--app={url}',
            ]
            # Firefox uses -profile <dir>; we mirror the chromium marker so
            # both can be killed by the single pkill above.
            firefox_args = ['--safe-mode', '-profile', kc_user_data_dir, '--kiosk', url]

            browser_commands = [
                ('chromium-browser', chrome_args),
                ('/usr/bin/chromium-browser', chrome_args),
                ('/usr/bin/google-chrome', chrome_args),
                ('/usr/local/bin/browser', []),
                ('/usr/bin/firefox-esr', firefox_args),
                ('/usr/bin/firefox', firefox_args),
                ('firefox-esr', firefox_args),
                ('firefox', firefox_args),
            ]

            browser_cmd = None
            browser_args = []
            for cmd, args in browser_commands:
                if os.path.exists(cmd) or subprocess.run(['which', cmd], capture_output=True).returncode == 0:
                    browser_cmd = cmd
                    browser_args = args
                    break

            if not browser_cmd:
                self.send_error_response('No Chrome browser found. Download may have failed.')
                return

            # If we fell through to /usr/local/bin/browser (no args defined),
            # append the URL so it still navigates somewhere.
            cmd_list = [browser_cmd] + browser_args
            if browser_cmd == '/usr/local/bin/browser':
                cmd_list.append(url)

            process = subprocess.Popen(
                cmd_list,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            # Give it a moment to start
            time.sleep(1)

            if process.poll() is None:  # Process is still running
                self.send_success_response(f'✅ Chrome opened with {url} (PID: {process.pid})')
            else:
                self.send_error_response('Chrome process exited immediately')

        except FileNotFoundError:
            self.send_error_response('Chrome not found. Please install Chrome first.')
        except Exception as e:
            self.send_error_response(f'Error opening localhost in Chrome: {str(e)}')

    def route_apps_pin_delete(self, port):
        """`DELETE /api/apps/pins/{port}` — the port is an int to the handler.

        The chain did the `int()` at the dispatch site; the table hands over
        the captured string. Same shape as the memory unlink adapter.
        """
        self._handle_apps_pin_delete(int(port))


ROUTES = RouteTable()

ROUTES.add('GET', '/api/apps', '_handle_apps_list')
ROUTES.add('GET', '/api/claude/apps/session', 'handle_app_session_mint')

ROUTES.add('POST', '/api/apps/pins', '_handle_apps_pin_create')
ROUTES.add('DELETE', re.compile(r'^/api/apps/pins/(\d+)$'),
           'route_apps_pin_delete')

# The legacy browser launchers. /api/launch-firefox and /api/test-firefox are
# back-compat aliases onto the same two handlers, preserved verbatim.
ROUTES.add('POST', '/api/launch-chrome', 'launch_chrome')
ROUTES.add('POST', '/api/launch-firefox', 'launch_chrome')
ROUTES.add('POST', '/api/test-chrome', 'test_chrome')
ROUTES.add('POST', '/api/test-firefox', 'test_chrome')
ROUTES.add('POST', '/api/open-localhost', 'open_localhost')
