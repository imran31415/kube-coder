#!/usr/bin/env python3
"""A stand-in workspace for measuring the desktop app (#833). Not a test of
server.py — just enough of its API to drive the app and COUNT its requests.

    GET  /api/events          SSE: `ready`, a keep-alive every 15 s, and any
                              event POSTed to /__emit. Answers 503 (plain
                              text, like a bare ingress) while the stream is
                              switched off.
    GET  /api/feed            one waiting board review (or more via /__emit)
    GET  /api/keeper/status   404: no keeper in front of this "workspace"
    *    /api/...             `{}` / `[]` so every screen renders
    POST /__emit?type=feed.item        publish an event (feed.item adds a row)
    POST /__stream?on=0|1              switch the event stream off / on
    GET  /__stats                      request counts per path, then reset

Every /api request must carry `Authorization: Bearer <token>`; anything else
is a 401 and is counted as `unauthorized`, so the run also shows the token
reaching nothing but this host.

Usage: fake-workspace.py [port] [token]      (defaults 6099, test-token)
"""
import collections
import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 6099
TOKEN = sys.argv[2] if len(sys.argv) > 2 else 'test-token'

lock = threading.Lock()
counts = collections.Counter()
subscribers = set()
stream_on = True
feed = [{
    'id': 'f1', 'ts': time.time(), 'kind': 'board', 'title': 'Review: post a PR comment',
    'body_md': 'An agent wants to comment on #42', 'source': 'board:demo', 'project_id': '',
    'links': [{'label': 'Review', 'ref': 'board:demo:item-1'}], 'waiting': True, 'read': False,
}]


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _json(self, body, status=200):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authed(self, path):
        if self.headers.get('Authorization') == f'Bearer {TOKEN}':
            return True
        with lock:
            counts['unauthorized'] += 1
        self._json({'error': 'Unauthorized'}, 401)
        return False

    def do_POST(self):
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        if u.path == '/__emit':
            etype = (q.get('type') or ['feed.item'])[0]
            data = {}
            if etype == 'feed.item':
                n = len(feed) + 1
                row = dict(feed[0], id=f'f{n}', ts=time.time(), title=f'Review #{n}',
                           links=[{'label': 'Review', 'ref': f'board:demo:item-{n}'}])
                feed.insert(0, row)
                data = {'id': row['id'], 'kind': 'board', 'project_id': ''}
            for sq in list(subscribers):
                sq.put((etype, data))
            return self._json({'ok': True})
        if u.path == '/__stream':
            global stream_on
            stream_on = (q.get('on') or ['1'])[0] == '1'
            if not stream_on:
                for sq in list(subscribers):
                    sq.put(None)
            return self._json({'stream': stream_on})
        if not u.path.startswith('/api/') or not self._authed(u.path):
            return
        self._count('POST', u)
        length = int(self.headers.get('Content-Length') or 0)
        self.rfile.read(length)
        if u.path.startswith('/api/feed/') and u.path.endswith('/read'):
            fid = u.path.split('/')[3]
            for r in feed:
                if r['id'] == fid:
                    r['read'] = True
        self._json({'ok': True})

    def _count(self, method, u):
        with lock:
            counts[f'{method} {u.path}' + ('?limit=200' if 'limit=200' in u.query else '')] += 1

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == '/__stats':
            with lock:
                snap = dict(counts)
                counts.clear()
            return self._json(snap)
        if not u.path.startswith('/api/') or not self._authed(u.path):
            if not u.path.startswith('/api/'):
                self._json({'error': 'not found'}, 404)
            return
        self._count('GET', u)
        if u.path == '/api/events':
            return self._events()
        if u.path == '/api/keeper/status':
            return self._json({'error': 'not found'}, 404)
        if u.path == '/api/feed':
            return self._json({'items': feed})
        if u.path == '/api/feed/unread_count':
            return self._json({'count': sum(1 for r in feed if not r['read'])})
        if u.path == '/api/claude/tasks':
            return self._json({'tasks': []})
        self._json({})

    def _events(self):
        if not stream_on:
            raw = b'<html>503 Service Temporarily Unavailable</html>'
            self.send_response(503)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()
        sq = queue.Queue()
        subscribers.add(sq)
        try:
            self.wfile.write(b'event: ready\ndata: {}\n\n')
            self.wfile.flush()
            while True:
                try:
                    ev = sq.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b': keep-alive\n\n')
                    self.wfile.flush()
                    continue
                if ev is None:
                    return
                self.wfile.write(f'event: {ev[0]}\ndata: {json.dumps(ev[1])}\n\n'.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            subscribers.discard(sq)
            self.close_connection = True


if __name__ == '__main__':
    print(f'fake workspace on http://127.0.0.1:{PORT} (token {TOKEN})', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
