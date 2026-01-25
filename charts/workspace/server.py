#!/usr/bin/env python3
import http.server
import socketserver
import subprocess
import os
import json
import time
import re

# Alert thresholds for metrics
ALERT_THRESHOLDS = {
    'cpu': {'warning': 70, 'critical': 90},
    'memory': {'warning': 80, 'critical': 95},
    'disk': {'warning': 80, 'critical': 90}
}

class MetricsCollector:
    """Collects system metrics from /proc filesystem and os.statvfs"""

    @staticmethod
    def get_cpu_usage():
        """Get CPU usage percentage using /proc/stat"""
        try:
            def read_cpu_times():
                with open('/proc/stat', 'r') as f:
                    line = f.readline()
                    parts = line.split()
                    # cpu user nice system idle iowait irq softirq steal guest guest_nice
                    if parts[0] == 'cpu':
                        times = [int(x) for x in parts[1:]]
                        idle = times[3] + times[4]  # idle + iowait
                        total = sum(times)
                        return idle, total
                return 0, 0

            idle1, total1 = read_cpu_times()
            time.sleep(0.5)
            idle2, total2 = read_cpu_times()

            idle_delta = idle2 - idle1
            total_delta = total2 - total1

            if total_delta == 0:
                usage_percent = 0.0
            else:
                usage_percent = ((total_delta - idle_delta) / total_delta) * 100

            # Count CPU cores
            cores = 0
            with open('/proc/stat', 'r') as f:
                for line in f:
                    if line.startswith('cpu') and line[3].isdigit():
                        cores += 1

            return {
                'usage_percent': round(usage_percent, 1),
                'cores': cores if cores > 0 else 1
            }
        except Exception as e:
            return {'usage_percent': 0.0, 'cores': 1, 'error': str(e)}

    @staticmethod
    def get_memory_usage():
        """Get memory usage from /proc/meminfo"""
        try:
            meminfo = {}
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    parts = line.split()
                    key = parts[0].rstrip(':')
                    value = int(parts[1])  # Value in kB
                    meminfo[key] = value

            total_kb = meminfo.get('MemTotal', 0)
            available_kb = meminfo.get('MemAvailable', meminfo.get('MemFree', 0))
            used_kb = total_kb - available_kb

            total_mb = total_kb / 1024
            used_mb = used_kb / 1024
            available_mb = available_kb / 1024

            percent = (used_kb / total_kb * 100) if total_kb > 0 else 0

            return {
                'total_mb': round(total_mb, 1),
                'used_mb': round(used_mb, 1),
                'available_mb': round(available_mb, 1),
                'percent': round(percent, 1)
            }
        except Exception as e:
            return {'total_mb': 0, 'used_mb': 0, 'available_mb': 0, 'percent': 0, 'error': str(e)}

    @staticmethod
    def get_disk_usage():
        """Get disk usage for /home/dev"""
        try:
            path = '/home/dev'
            if not os.path.exists(path):
                path = '/'

            stat = os.statvfs(path)
            total_bytes = stat.f_blocks * stat.f_frsize
            available_bytes = stat.f_bavail * stat.f_frsize
            used_bytes = total_bytes - available_bytes

            total_gb = total_bytes / (1024 ** 3)
            used_gb = used_bytes / (1024 ** 3)
            available_gb = available_bytes / (1024 ** 3)

            percent = (used_bytes / total_bytes * 100) if total_bytes > 0 else 0

            return {
                'total_gb': round(total_gb, 1),
                'used_gb': round(used_gb, 1),
                'available_gb': round(available_gb, 1),
                'percent': round(percent, 1),
                'path': path
            }
        except Exception as e:
            return {'total_gb': 0, 'used_gb': 0, 'available_gb': 0, 'percent': 0, 'path': '/home/dev', 'error': str(e)}

    @staticmethod
    def get_alerts(cpu, memory, disk):
        """Generate alerts based on current metrics"""
        alerts = []

        if cpu.get('usage_percent', 0) >= ALERT_THRESHOLDS['cpu']['critical']:
            alerts.append({'type': 'critical', 'resource': 'cpu', 'message': f"CPU usage at {cpu['usage_percent']}%"})
        elif cpu.get('usage_percent', 0) >= ALERT_THRESHOLDS['cpu']['warning']:
            alerts.append({'type': 'warning', 'resource': 'cpu', 'message': f"CPU usage at {cpu['usage_percent']}%"})

        if memory.get('percent', 0) >= ALERT_THRESHOLDS['memory']['critical']:
            alerts.append({'type': 'critical', 'resource': 'memory', 'message': f"Memory usage at {memory['percent']}%"})
        elif memory.get('percent', 0) >= ALERT_THRESHOLDS['memory']['warning']:
            alerts.append({'type': 'warning', 'resource': 'memory', 'message': f"Memory usage at {memory['percent']}%"})

        if disk.get('percent', 0) >= ALERT_THRESHOLDS['disk']['critical']:
            alerts.append({'type': 'critical', 'resource': 'disk', 'message': f"Disk usage at {disk['percent']}%"})
        elif disk.get('percent', 0) >= ALERT_THRESHOLDS['disk']['warning']:
            alerts.append({'type': 'warning', 'resource': 'disk', 'message': f"Disk usage at {disk['percent']}%"})

        return alerts

    @staticmethod
    def get_all_metrics():
        """Return all metrics as a dictionary"""
        cpu = MetricsCollector.get_cpu_usage()
        memory = MetricsCollector.get_memory_usage()
        disk = MetricsCollector.get_disk_usage()
        alerts = MetricsCollector.get_alerts(cpu, memory, disk)

        return {
            'cpu': cpu,
            'memory': memory,
            'disk': disk,
            'alerts': alerts,
            'timestamp': time.time()
        }


class GitHubManager:
    """Handles GitHub authentication and configuration"""

    SSH_DIR = os.path.expanduser('~/.ssh')
    GH_CONFIG_DIR = os.path.expanduser('~/.config/gh')

    @staticmethod
    def get_ssh_status():
        """Check if SSH key exists and get its details"""
        key_path = os.path.join(GitHubManager.SSH_DIR, 'id_ed25519')
        pub_key_path = key_path + '.pub'

        if not os.path.exists(pub_key_path):
            return {'configured': False}

        try:
            with open(pub_key_path, 'r') as f:
                public_key = f.read().strip()

            # Get fingerprint
            result = subprocess.run(
                ['ssh-keygen', '-lf', pub_key_path],
                capture_output=True, text=True
            )
            fingerprint = result.stdout.split()[1] if result.returncode == 0 else 'unknown'

            return {
                'configured': True,
                'key_type': 'ed25519',
                'key_fingerprint': fingerprint,
                'public_key': public_key
            }
        except Exception as e:
            return {'configured': False, 'error': str(e)}

    @staticmethod
    def generate_ssh_key(email):
        """Generate new SSH key pair"""
        key_path = os.path.join(GitHubManager.SSH_DIR, 'id_ed25519')
        os.makedirs(GitHubManager.SSH_DIR, mode=0o700, exist_ok=True)

        # Remove existing key if present
        for ext in ['', '.pub']:
            path = key_path + ext
            if os.path.exists(path):
                os.remove(path)

        result = subprocess.run([
            'ssh-keygen', '-t', 'ed25519', '-C', email,
            '-f', key_path, '-N', ''
        ], capture_output=True, text=True)

        if result.returncode != 0:
            raise Exception(f"Failed to generate key: {result.stderr}")

        # Add GitHub config to SSH config file
        config_path = os.path.join(GitHubManager.SSH_DIR, 'config')
        github_config = """
Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
"""
        # Check if config exists and already has github.com
        existing_config = ''
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                existing_config = f.read()

        if 'github.com' not in existing_config:
            with open(config_path, 'a') as f:
                f.write(github_config)
            os.chmod(config_path, 0o600)

        return GitHubManager.get_ssh_status()

    @staticmethod
    def get_gh_cli_status():
        """Check gh CLI authentication status"""
        try:
            result = subprocess.run(
                ['gh', 'auth', 'status', '--hostname', 'github.com'],
                capture_output=True, text=True
            )

            if result.returncode != 0:
                return {'installed': True, 'authenticated': False}

            # Parse output to get username (gh writes to stderr)
            output = result.stderr + result.stdout
            username = None
            for line in output.split('\n'):
                if 'Logged in to github.com' in line:
                    # Try to extract username
                    if 'account' in line:
                        parts = line.split('account')
                        if len(parts) > 1:
                            username = parts[1].strip().split()[0].strip('()')
                    break

            return {
                'installed': True,
                'authenticated': True,
                'username': username
            }
        except FileNotFoundError:
            return {'installed': False, 'authenticated': False}
        except Exception as e:
            return {'installed': True, 'authenticated': False, 'error': str(e)}

    @staticmethod
    def start_device_flow():
        """Start gh auth device flow - returns instructions for manual auth"""
        # We can't truly start interactive device flow from a server
        # Instead, provide instructions for the user
        return {
            'instructions': 'Run the following command in the terminal to authenticate:',
            'command': 'gh auth login --hostname github.com --git-protocol https --web',
            'manual_steps': [
                '1. Open Terminal from the dashboard',
                '2. Run: gh auth login',
                '3. Select GitHub.com',
                '4. Select HTTPS',
                '5. Authenticate with browser when prompted',
                '6. Return here and click "Check Status"'
            ]
        }

    @staticmethod
    def get_git_config():
        """Get git global config"""
        try:
            name_result = subprocess.run(
                ['git', 'config', '--global', 'user.name'],
                capture_output=True, text=True
            )
            email_result = subprocess.run(
                ['git', 'config', '--global', 'user.email'],
                capture_output=True, text=True
            )
            return {
                'user_name': name_result.stdout.strip() if name_result.returncode == 0 else '',
                'user_email': email_result.stdout.strip() if email_result.returncode == 0 else ''
            }
        except Exception as e:
            return {'user_name': '', 'user_email': '', 'error': str(e)}

    @staticmethod
    def set_git_config(name, email):
        """Set git global config"""
        try:
            subprocess.run(['git', 'config', '--global', 'user.name', name], check=True)
            subprocess.run(['git', 'config', '--global', 'user.email', email], check=True)
            return GitHubManager.get_git_config()
        except Exception as e:
            return {'error': str(e)}

    @staticmethod
    def get_full_status():
        """Get combined GitHub status"""
        return {
            'ssh': GitHubManager.get_ssh_status(),
            'gh_cli': GitHubManager.get_gh_cli_status(),
            'git_config': GitHubManager.get_git_config()
        }


class BrowserHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ["/", "/dashboard", "/dashboard/", "/oauth", "/oauth/"]:
            self.path = "/dashboard.html"
        elif self.path in ["/browser", "/browser/"]:
            self.path = "/index.html"
        elif self.path == "/health":
            self.send_health_check()
            return
        elif self.path == "/health/vscode":
            self.send_vscode_health()
            return
        elif self.path == "/health/terminal":
            self.send_terminal_health()
            return
        elif self.path == "/health/browser":
            self.send_browser_health()
            return
        elif self.path == "/metrics":
            self.send_metrics()
            return
        elif self.path == "/api/github/status":
            self.send_github_status()
            return
        elif self.path == "/api/github/config":
            self.send_git_config()
            return
        elif self.path == "/vnc" or self.path == "/vnc/":
            self.send_vnc_viewer()
            return
        elif self.path == "/vnc-proxy" or self.path == "/vnc-proxy/":
            self.redirect_to_vnc()
            return
        elif self.path.startswith("/vnc/"):
            self.proxy_vnc_request()
            return
        super().do_GET()
    
    def check_auth(self):
        """Check if request has proper authentication headers"""
        auth_header = self.headers.get('Authorization', '')
        # If we have nginx auth, the user is already authenticated
        # We can also check for specific headers nginx sets
        remote_user = self.headers.get('Remote-User', '')
        if auth_header or remote_user:
            return True
        return False
    
    def send_vnc_viewer(self):
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
        # Redirect to the noVNC URL running on localhost:6081
        import urllib.request
        try:
            # Proxy the request to the local noVNC server
            vnc_url = "http://localhost:6081/vnc.html?autoconnect=true&resize=scale"
            with urllib.request.urlopen(vnc_url) as response:
                content = response.read()
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(content)
        except Exception as e:
            error_html = f'''<!DOCTYPE html>
<html>
<head><title>VNC Connection Error</title></head>
<body>
    <h1>VNC Connection Error</h1>
    <p>Unable to connect to VNC server: {str(e)}</p>
    <p><a href="/browser/">← Back to Browser Controls</a></p>
    <p>Make sure a browser is launched first, then try again.</p>
</body>
</html>'''
            self.send_response(500)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(error_html.encode())
    
    def proxy_vnc_request(self):
        # Proxy requests to the local noVNC server
        import urllib.request
        import urllib.parse
        try:
            # Remove /vnc/ from the path and proxy to localhost:6081
            vnc_path = self.path[5:]  # Remove '/vnc/' prefix
            vnc_url = f"http://localhost:6081/{vnc_path}"
            
            # Add query string if present
            if '?' in self.path:
                vnc_url = f"http://localhost:6081/{vnc_path}"
            
            with urllib.request.urlopen(vnc_url) as response:
                content = response.read()
                content_type = response.headers.get('Content-Type', 'text/html')
                self.send_response(200)
                self.send_header('Content-type', content_type)
                self.end_headers()
                self.wfile.write(content)
        except Exception as e:
            error_html = f'''<!DOCTYPE html>
<html>
<head><title>VNC Proxy Error</title></head>
<body>
    <h1>VNC Proxy Error</h1>
    <p>Error accessing VNC: {str(e)}</p>
    <p>Path: {self.path}</p>
    <p>VNC URL: {vnc_url if 'vnc_url' in locals() else 'N/A'}</p>
</body>
</html>'''
            self.send_response(500)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(error_html.encode())
    
    def do_POST(self):
        try:
            # Handle both /api/* and /browser/api/* and /oauth/browser/api/* paths
            path = self.path.replace('/browser', '').replace('/oauth', '')
            
            if path == "/api/launch-chrome":
                self.launch_chrome()
            elif path == "/api/open-localhost":
                self.open_localhost()
            elif path == "/api/test-chrome":
                self.test_chrome()
            # Keep Firefox endpoints for backward compatibility
            elif path == "/api/launch-firefox":
                self.launch_chrome()
            elif path == "/api/test-firefox":
                self.test_chrome()
            # GitHub configuration endpoints
            elif path == "/api/github/ssh/generate":
                self.handle_ssh_generate()
            elif path == "/api/github/config":
                self.handle_git_config_post()
            elif path == "/api/github/cli/login-url":
                self.handle_gh_login_instructions()
            elif path == "/api/github/cli/complete-auth":
                self.handle_gh_check_auth()
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(f'API endpoint not found. Received: {self.path}'.encode())
        except Exception as e:
            self.send_error_response(f'Server error: {str(e)}')
    
    def send_success_response(self, message):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(message.encode())
    
    def send_error_response(self, message):
        self.send_response(500)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(message.encode())
    
    def send_health_check(self):
        """Overall health check endpoint - always returns 200 to avoid blocking"""
        vscode_status = self.check_service_health('localhost', 8080)
        terminal_status = self.check_service_health('localhost', 7681)
        browser_status = self.check_service_health('localhost', 6081)
        
        health_data = {
            'status': 'healthy' if all([vscode_status, terminal_status, browser_status]) else 'degraded',
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

    def send_metrics(self):
        """Send system metrics (CPU, memory, disk) as JSON"""
        metrics = MetricsCollector.get_all_metrics()

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(metrics).encode())

    def send_github_status(self):
        """Send combined GitHub status as JSON"""
        status = GitHubManager.get_full_status()

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(status).encode())

    def send_git_config(self):
        """Send git config as JSON"""
        config = GitHubManager.get_git_config()

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(config).encode())

    def handle_ssh_generate(self):
        """Handle SSH key generation request"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body) if body else {}

            email = data.get('email', 'user@example.com')
            result = GitHubManager.generate_ssh_key(email)

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def handle_git_config_post(self):
        """Handle git config update request"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body) if body else {}

            name = data.get('name', '')
            email = data.get('email', '')

            if not name or not email:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Name and email are required'}).encode())
                return

            result = GitHubManager.set_git_config(name, email)

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def handle_gh_login_instructions(self):
        """Return instructions for gh CLI authentication"""
        instructions = GitHubManager.start_device_flow()

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(instructions).encode())

    def handle_gh_check_auth(self):
        """Check if gh CLI authentication is complete"""
        status = GitHubManager.get_gh_cli_status()

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(status).encode())

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
    
    def test_chrome(self):
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
                    # xdpyinfo failed, but check if Xvfb process is running instead
                    xvfb_check = subprocess.run(['pgrep', 'Xvfb'], capture_output=True)
                    if xvfb_check.returncode != 0:
                        self.send_error_response(f'X11 display {display} not available')
                        return
            except (subprocess.TimeoutExpired, FileNotFoundError):
                # xdpyinfo not available or timed out, check if Xvfb process is running
                xvfb_check = subprocess.run(['pgrep', 'Xvfb'], capture_output=True)
                if xvfb_check.returncode != 0:
                    self.send_error_response(f'X11 display {display} not available (Xvfb not running)')
                    return
            
            self.send_success_response(f'✅ Browser found at: {browser_path}\n✅ X11 display {display} available')
            
        except Exception as e:
            self.send_error_response(f'Test failed: {str(e)}')
    
    def launch_chrome(self):
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
        try:
            env = os.environ.copy()
            env['DISPLAY'] = ':99'
            
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
            
            # Launch browser with localhost URL
            cmd_list = [browser_cmd] + browser_args + ['--new-window', 'http://localhost:8080']
            process = subprocess.Popen(
                cmd_list, 
                env=env,
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )
            
            # Give it a moment to start
            time.sleep(1)
            
            if process.poll() is None:  # Process is still running
                self.send_success_response(f'✅ Chrome opened with localhost:8080 (PID: {process.pid})')
            else:
                self.send_error_response('Chrome process exited immediately')
                
        except FileNotFoundError:
            self.send_error_response('Chrome not found. Please install Chrome first.')
        except Exception as e:
            self.send_error_response(f'Error opening localhost in Chrome: {str(e)}')

if __name__ == "__main__":
    # Change to the directory containing our files
    os.chdir('/tmp/browser')
    
    print("Starting Browser API Server on port 6080...")
    print("Available endpoints:")
    print("  GET  /           - Browser interface")
    print("  POST /api/launch-chrome - Launch Chrome")
    print("  POST /api/open-localhost - Open localhost:8080 in Chrome")
    print("  POST /api/test-chrome   - Test Chrome installation")
    print("  POST /api/launch-firefox - Launch Chrome (legacy endpoint)")
    print("  POST /api/test-firefox   - Test Chrome (legacy endpoint)")
    
    with socketserver.TCPServer(("", 6080), BrowserHandler) as httpd:
        httpd.serve_forever()