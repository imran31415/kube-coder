"""Workspace status surface: liveness, health, metrics, identity, VNC (#100).

The first domain lifted out of server.py's BrowserHandler. It was picked
because its fifteen GET routes are one *contiguous* run of elif branches in
do_GET — nothing had to move across anything else for the table below to
reproduce the old order exactly.

Two things about these routes are easy to lose and are therefore spelled out
in the table rather than left to a reader's memory of the chain:

  * Most of them match the RAW request path. `/livez`, `/health*`, `/metrics`
    and `/vnc*` have always been compared against `self.path`, so `/oauth/livez`
    and `/livez?probe=1` do not reach them — they fall through to the SPA
    history check, which 404s them because those prefixes are in
    `NON_SPA_PREFIXES`. Only `/metrics/prometheus`, `/api/github/*` and
    `/api/workspace/version` match the normalized path, because the dashboard
    SPA prefixes its own calls with `/oauth`.
  * `/vnc/` exactly is the viewer page; `/vnc/<anything>` is the noVNC proxy.

The handler methods are mixed into BrowserHandler, so they keep using the same
`self.send_json` / `self.check_claude_auth` helpers they always did; the move
is a pure relocation. Managers that still live in server.py are reached
through `handlers.server` — see handlers/__init__.py for why that injection
exists instead of an `import server`.
"""

import html
import json
import re
import sys
import time

import handlers
from handlers.routing import RouteTable


class SystemRoutes:
    """BrowserHandler methods backing `ROUTES`."""

    def send_livez(self):
        """Liveness probe — proves the HTTP server thread is alive and can
        answer, nothing more. Deliberately does ZERO blocking work: no socket
        connects to sub-services (see send_health_check), no auth, no disk, no
        JSON. On a 2-CPU pod a busy assistant task + many tmux-streaming
        handler threads can starve the GIL enough that a heavier handler can't
        finish inside the 10s liveness timeout for 3 straight probes (~90s),
        and the kubelet then SIGTERMs the container — killing the user's live
        tmux + tasks. A handler this cheap needs the GIL for only microseconds,
        so it returns even under heavy contention. Sub-service status belongs
        to /health (readiness) and the /health/* detail endpoints."""
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(b'ok')

    def send_health_check(self):
        """Overall health check endpoint - always returns 200 to avoid blocking"""
        vscode_status = self.check_service_health('localhost', 8080)
        terminal_status = self.check_service_health('localhost', 7681)
        browser_status = self.check_service_health('localhost', 6081)
        
        health_data = {
            'status': 'healthy' if (terminal_status and browser_status) else 'degraded',
            'services': {
                'vscode': {'status': 'up' if vscode_status else 'down', 'port': 8080},
                'terminal': {'status': 'up' if terminal_status else 'down', 'port': 7681},
                'browser': {'status': 'up' if browser_status else 'down', 'port': 6081}
            },
            'timestamp': time.time()
        }
        
        # Always return 200 to avoid blocking the service
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(health_data).encode())

    def send_vscode_health(self):
        """VS Code health check - always returns 200"""
        status = self.check_service_health('localhost', 8080)
        response = {'service': 'vscode', 'status': 'up' if status else 'down', 'port': 8080}
        
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

    def send_terminal_health(self):
        """Terminal health check - always returns 200"""
        status = self.check_service_health('localhost', 7681)
        response = {'service': 'terminal', 'status': 'up' if status else 'down', 'port': 7681}
        
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

    def send_browser_health(self):
        """Browser/VNC health check - always returns 200"""
        vnc_status = self.check_service_health('localhost', 5900)  # x11vnc
        websockify_status = self.check_service_health('localhost', 6081)  # websockify
        
        status = vnc_status and websockify_status
        response = {
            'service': 'browser',
            'status': 'up' if status else 'down',
            'components': {
                'vnc': 'up' if vnc_status else 'down',
                'websockify': 'up' if websockify_status else 'down'
            }
        }
        
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

    def check_service_health(self, host, port):
        """Check if a service is listening on the given port"""
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                result = s.connect_ex((host, port))
                return result == 0
        except Exception:
            return False

    def send_metrics(self):
        """Send system metrics (CPU, memory, disk) as JSON.
        Auth-gated to avoid double-duty as an unauthenticated workload
        side-channel — public-demo callers get through via AUTH_MODE=none."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        metrics = handlers.server.MetricsCollector.get_all_metrics()

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(metrics).encode())

    def send_prometheus_metrics(self):
        """GET /metrics/prometheus — the platform's own metrics in Prometheus
        text exposition format (#105).

        A SEPARATE PATH from the JSON /metrics above, on purpose. That endpoint
        is what the dashboard SPA's Metrics page reads; content-negotiating the
        two off one URL would put the Metrics page one `Accept`-header change
        away from rendering nothing, and would need a correct `Vary: Accept`
        to survive the ingress and oauth2-proxy in front of this server. A
        scraper's `metrics_path` is a one-line config field, so the separate
        path costs nothing and cannot break the SPA. See
        PrometheusMetricsCollector for what is exposed and why.

        Same auth gate as the JSON endpoint — it reports the same underlying
        facts, so anything that could read it there can read it here. A
        Prometheus scrape authenticates with the workspace's Claude Task API
        token as a Bearer credential (`authorization` / `bearerTokenSecret` in
        a ServiceMonitor).
        """
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if not handlers.server._PROMETHEUS_AVAILABLE:
            self.send_json({'error': 'prometheus exposition unavailable'}, 503)
            return
        try:
            body = handlers.server.PrometheusMetricsCollector.render().encode('utf-8')
        except Exception as e:
            # render() already isolates each section, so reaching here means the
            # document itself could not be built — serve nothing rather than a
            # half-formed exposition Prometheus would reject wholesale anyway.
            print(f'[prom-metrics] render failed: {type(e).__name__}: {e}',
                  file=sys.stderr)
            self.send_json({'error': 'metrics unavailable'}, 500)
            return
        self.send_response(200)
        self.send_header('Content-type', handlers.server.prom.CONTENT_TYPE)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(body)

    def send_github_status(self):
        """Send combined GitHub status as JSON.
        Strictly auth-gated (allow_none_mode=False) — the response leaks
        the SSH public-key fingerprint, gh CLI username, and git
        name/email, none of which should ever surface on a public demo."""
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        status = handlers.server.GitHubManager.get_full_status()

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(status).encode())

    def send_git_config(self):
        """Send git config as JSON. Strictly auth-gated (allow_none_mode=False)
        — exposes the operator's git name + email."""
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        config = handlers.server.GitHubManager.get_git_config()

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(config).encode())

    def send_workspace_version(self):
        """Current vs latest workspace version, brokered from the controller.
        Auth-gated (exposes the workspace's image version). Returns
        {available:false} cleanly when self-serve updates aren't wired, so the
        SPA can simply hide the section instead of erroring."""
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if not handlers.server.UpdateManager.enabled():
            self.send_json({'available': False,
                            'reason': 'self-serve updates not configured'})
            return
        status, payload = handlers.server.UpdateManager.get_version()
        if status == 200:
            self.send_json({'available': True, **payload})
        else:
            self.send_json({'available': True, 'error': payload.get('error', 'controller error')},
                           status if status >= 400 else 502)

    def send_vnc_viewer(self):
        # Defense-in-depth: oauth2-proxy should already have rejected an
        # unauth'd visitor, but if this handler is ever reached directly
        # (e.g. a misconfigured ingress) refuse rather than render the
        # iframe URL anyway. The deeper /vnc/<path> proxy IS authed; this
        # wrapper page used to slip through.
        if not self.check_claude_auth():
            self.send_response(401)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Unauthorized')
            return
        # Instead of embedding, redirect to the noVNC URL directly
        host = self.headers.get('Host', 'localhost').split(':')[0]
        vnc_url = f"https://{host}/vnc-direct/vnc.html?host={host}&port=6081&autoconnect=true&resize=scale"
        
        vnc_html = f'''<!DOCTYPE html>
<html>
<head>
    <title>VNC Viewer</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; text-align: center; }}
        .container {{ max-width: 600px; margin: 0 auto; }}
        .btn {{ background: #007cba; color: white; border: none; padding: 12px 24px; margin: 10px; border-radius: 4px; text-decoration: none; display: inline-block; }}
        .btn:hover {{ background: #005a8b; }}
        .warning {{ background: #fff3cd; border: 1px solid #ffeaa7; color: #856404; padding: 10px; border-radius: 4px; margin: 10px 0; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🖥️ Remote Desktop Viewer</h1>
        <div class="warning">
            <strong>🔒 Secure Access:</strong> This VNC viewer is protected by authentication.
            You must be logged into this workspace to access the remote desktop.
        </div>
        <p>Click the button below to open the VNC viewer in a new window:</p>
        <a href="{vnc_url}" target="_blank" class="btn">Open VNC Viewer</a>
        <p><small>If the VNC viewer doesn't load, make sure you've launched a browser first.</small></p>
        <p><a href="/browser/">← Back to Browser Controls</a></p>
    </div>
</body>
</html>'''
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(vnc_html.encode())

    def redirect_to_vnc(self):
        # Defense-in-depth — see send_vnc_viewer above. This handler
        # actually proxies localhost:6081 content, so unauth'd access
        # would have exposed the VNC HTML directly.
        if not self.check_claude_auth():
            self.send_response(401)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Unauthorized')
            return
        # Redirect to the noVNC URL running on localhost:6081
        import urllib.request
        try:
            # Proxy the request to the local noVNC server
            vnc_url = "http://localhost:6081/vnc.html?autoconnect=true&resize=scale"
            with urllib.request.urlopen(vnc_url, timeout=10) as response:
                content = response.read()
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(content)
        except Exception as e:
            # Escape so a crafted upstream error message can't inject HTML
            # into this authenticated origin (reflected XSS).
            error_html = f'''<!DOCTYPE html>
<html>
<head><title>VNC Connection Error</title></head>
<body>
    <h1>VNC Connection Error</h1>
    <p>Unable to connect to VNC server: {html.escape(str(e))}</p>
    <p><a href="/browser/">← Back to Browser Controls</a></p>
    <p>Make sure a browser is launched first, then try again.</p>
</body>
</html>'''
            self.send_response(500)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(error_html.encode())

    def proxy_vnc_request(self):
        # Proxy requests to the local noVNC server.
        # Gate behind the same auth as the rest of the dashboard — the VNC
        # iframe is loaded from an already-authenticated SPA page, so callers
        # will always carry OAuth2 headers or a Bearer token.
        if not self.check_claude_auth():
            self.send_response(401)
            self.end_headers()
            return

        import urllib.request
        import urllib.parse
        vnc_url = None
        try:
            # Split off path + query; reject anything with control characters
            # before we paste it into a URL. self.path is attacker-controllable.
            raw = self.path[5:]  # strip "/vnc/"
            if any(ord(c) < 0x20 or c in ('\x7f',) for c in raw):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'invalid characters in path')
                return
            if '?' in raw:
                path_part, query_part = raw.split('?', 1)
            else:
                path_part, query_part = raw, ''

            # Normalize and confine: drop empty / "." / ".." segments so the
            # caller cannot climb above /. The destination host is hardcoded
            # to localhost:6081, but a normalized path keeps proxied requests
            # to the shapes noVNC actually serves.
            segments = [
                seg for seg in path_part.split('/')
                if seg and seg not in ('.', '..')
            ]
            safe_path = '/'.join(
                urllib.parse.quote(urllib.parse.unquote(s), safe='') for s in segments
            )
            vnc_url = f"http://localhost:6081/{safe_path}"
            if query_part:
                vnc_url += f"?{query_part}"

            with urllib.request.urlopen(vnc_url, timeout=10) as response:
                content = response.read()
                content_type = response.headers.get('Content-Type', 'text/html')
                self.send_response(200)
                self.send_header('Content-type', content_type)
                self.end_headers()
                self.wfile.write(content)
        except Exception as e:
            safe_url = html.escape(vnc_url) if vnc_url else 'N/A'
            error_html = f'''<!DOCTYPE html>
<html>
<head><title>VNC Proxy Error</title></head>
<body>
    <h1>VNC Proxy Error</h1>
    <p>Error accessing VNC: {html.escape(str(e))}</p>
    <p>Path: {html.escape(self.path)}</p>
    <p>VNC URL: {safe_url}</p>
</body>
</html>'''
            self.send_response(500)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(error_html.encode())


#: Consulted by do_GET immediately after the SPA roots, which is exactly where
#: the elif chain these entries replace used to sit. Registration order is
#: match order.
ROUTES = RouteTable()

# Kubelet probes and the JSON metrics endpoint: raw-path only (see module
# docstring). /livez must stay the cheapest handler in the file.
ROUTES.add('GET', '/livez', 'send_livez', raw_path=True)
ROUTES.add('GET', '/health', 'send_health_check', raw_path=True)
ROUTES.add('GET', '/health/vscode', 'send_vscode_health', raw_path=True)
ROUTES.add('GET', '/health/terminal', 'send_terminal_health', raw_path=True)
ROUTES.add('GET', '/health/browser', 'send_browser_health', raw_path=True)
ROUTES.add('GET', '/metrics', 'send_metrics', raw_path=True)

# Normalized-path reads: the SPA prefixes these with /oauth in oauth2 mode, and
# a Prometheus scrape may carry a query string.
ROUTES.add('GET', '/metrics/prometheus', 'send_prometheus_metrics')
ROUTES.add('GET', '/api/github/status', 'send_github_status')
ROUTES.add('GET', '/api/github/config', 'send_git_config')
ROUTES.add('GET', '/api/workspace/version', 'send_workspace_version')

# ORDERING HAZARD: the `^/vnc/` prefix pattern below also matches a bare
# `/vnc/`, so the two exact viewer routes have to be registered ahead of it.
# `/vnc-proxy` does not start with `/vnc/`, so its position is free.
ROUTES.add('GET', '/vnc', 'send_vnc_viewer', raw_path=True)
ROUTES.add('GET', '/vnc/', 'send_vnc_viewer', raw_path=True)
ROUTES.add('GET', '/vnc-proxy', 'redirect_to_vnc', raw_path=True)
ROUTES.add('GET', '/vnc-proxy/', 'redirect_to_vnc', raw_path=True)
ROUTES.add('GET', re.compile(r'^/vnc/'), 'proxy_vnc_request', raw_path=True)
