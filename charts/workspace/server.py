#!/usr/bin/env python3
import http.server
import socketserver
import subprocess
import os
import json
import time

class BrowserHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ["/", "/dashboard", "/dashboard/"]:
            self.path = "/dashboard.html"
        elif self.path in ["/browser", "/browser/"]:
            self.path = "/index.html"
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
            # Handle both /api/* and /browser/api/* paths
            path = self.path.replace('/browser', '')
            
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