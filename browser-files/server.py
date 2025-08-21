#!/usr/bin/env python3
import http.server
import socketserver
import subprocess
import os
import json
import time

class BrowserHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ["/", "/browser"]:
            self.path = "/index.html"
        super().do_GET()
    
    def do_POST(self):
        try:
            if self.path == "/api/launch-firefox":
                self.launch_firefox()
            elif self.path == "/api/open-localhost":
                self.open_localhost()
            elif self.path == "/api/test-firefox":
                self.test_firefox()
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'API endpoint not found')
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
    
    def test_firefox(self):
        try:
            # Test Firefox installation
            result = subprocess.run(['which', 'firefox'], capture_output=True, text=True)
            if result.returncode != 0:
                self.send_error_response('Firefox not found in PATH')
                return
            
            firefox_path = result.stdout.strip()
            
            # Test Xvfb display
            display = os.environ.get('DISPLAY', ':99')
            result = subprocess.run(['xdpyinfo', '-display', display], 
                                   capture_output=True, text=True)
            if result.returncode != 0:
                self.send_error_response(f'X11 display {display} not available')
                return
            
            self.send_success_response(f'✅ Firefox found at: {firefox_path}\n✅ X11 display {display} available')
            
        except Exception as e:
            self.send_error_response(f'Test failed: {str(e)}')
    
    def launch_firefox(self):
        try:
            env = os.environ.copy()
            env['DISPLAY'] = ':99'
            
            # Launch Firefox in background
            process = subprocess.Popen(
                ['firefox', '--new-window'], 
                env=env,
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )
            
            # Give it a moment to start
            time.sleep(1)
            
            if process.poll() is None:  # Process is still running
                self.send_success_response(f'✅ Firefox launched successfully (PID: {process.pid})')
            else:
                self.send_error_response('Firefox process exited immediately')
                
        except FileNotFoundError:
            self.send_error_response('Firefox not found. Please install Firefox first.')
        except Exception as e:
            self.send_error_response(f'Error launching Firefox: {str(e)}')
    
    def open_localhost(self):
        try:
            env = os.environ.copy()
            env['DISPLAY'] = ':99'
            
            # Launch Firefox with localhost URL
            process = subprocess.Popen(
                ['firefox', '--new-window', 'http://localhost:8080'], 
                env=env,
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )
            
            # Give it a moment to start
            time.sleep(1)
            
            if process.poll() is None:  # Process is still running
                self.send_success_response(f'✅ Firefox opened with localhost:8080 (PID: {process.pid})')
            else:
                self.send_error_response('Firefox process exited immediately')
                
        except FileNotFoundError:
            self.send_error_response('Firefox not found. Please install Firefox first.')
        except Exception as e:
            self.send_error_response(f'Error opening localhost in Firefox: {str(e)}')

if __name__ == "__main__":
    # Change to the directory containing our files
    os.chdir('/tmp/browser')
    
    print("Starting Browser API Server on port 6080...")
    print("Available endpoints:")
    print("  GET  /           - Browser interface")
    print("  POST /api/launch-firefox - Launch Firefox")
    print("  POST /api/open-localhost - Open localhost:8080 in Firefox")
    print("  POST /api/test-firefox   - Test Firefox installation")
    
    with socketserver.TCPServer(("", 6080), BrowserHandler) as httpd:
        httpd.serve_forever()