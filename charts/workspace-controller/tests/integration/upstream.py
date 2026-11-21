"""Test-only upstream counting protected requests, including upgrade probes."""
import http.server
import json
import threading


class Handler(http.server.BaseHTTPRequestHandler):
    count = 0
    lock = threading.Lock()

    def do_GET(self):
        with self.lock:
            if self.path != '/count':
                Handler.count += 1
            count = Handler.count
        body = json.dumps({'protectedRequests': count}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)


http.server.ThreadingHTTPServer(('0.0.0.0', 8099), Handler).serve_forever()
