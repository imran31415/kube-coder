"""Restricted signup HTTP listener. No inheritance from the admin handler."""
import hashlib
import hmac
import http.server
import json
import re
import time
import urllib.parse
from observability_http import ObservedHTTP
from observability_logging import event
from signup_errors import SignupError


def signup_html(html):
    if b'<head>' not in html or b'kc-signup' not in html:
        raise ValueError('Signup UI is missing. Run make controller-web before deploying the controller chart.')
    return html.replace(b'<head>', b'<head><meta name="kc-signup" content="true">', 1)


def identity(headers):
    # Signup proxy maps the verified federated_claims.user_id into Groups.
    # The dedicated Dex deployment has exactly one connector: GitHub.
    owner = headers.get('X-Forwarded-Groups', '')
    login = headers.get('X-Forwarded-Preferred-Username', '')
    if not re.fullmatch(r'[1-9][0-9]{0,19}', owner) or not re.fullmatch(r'[a-zA-Z0-9](?:-?[a-zA-Z0-9]){0,38}', login):
        raise SignupError('unauthorized')
    return owner, login


def handler(service, origin, csrf_secret, index_html):
    class SignupHandler(ObservedHTTP, http.server.BaseHTTPRequestHandler):
        telemetry_service = 'controller'

        def send_json(self, data, status=200):
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def csrf(self, owner):
            cookie = self.headers.get('Cookie', '')
            if not cookie:
                raise SignupError('unauthorized')
            return hmac.new(csrf_secret.encode(), (owner + '\n' + cookie).encode(), hashlib.sha256).hexdigest()

        def do_GET(self):
            self.dispatch(False)

        def do_POST(self):
            self.dispatch(True)

        def dispatch(self, mutate):
            path = urllib.parse.urlsplit(self.path).path
            try:
                if not mutate and path in ('/', '/signup', '/signup/'):
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(index_html)
                    return
                if not mutate and path == '/api/signup/config':
                    self.send_json({'enabled': True})
                    return
                routes = {'/api/signup/workspace', '/api/signup/retry'} if mutate else {'/api/signup/me', '/api/signup/status'}
                if path not in routes:
                    self.send_json({'error': 'Not found.'}, 404)
                    return
                owner, login = identity(self.headers)
                if mutate:
                    if self.headers.get('Origin') != origin or not hmac.compare_digest(
                            self.headers.get('X-CSRF-Token', ''), self.csrf(owner)):
                        raise SignupError('forbidden')
                    length = int(self.headers.get('Content-Length', '0'))
                    if length < 0 or length > 1024 or self.headers.get('Transfer-Encoding'):
                        raise SignupError('invalid_request')
                    body = json.loads(self.rfile.read(length) or b'{}')
                    if body != {}:
                        raise SignupError('invalid_request')
                    record = service.create(owner, login) if path.endswith('/workspace') else service.retry(owner)
                    self.send_json({'operation': service.public(record)}, 202)
                elif path.endswith('/me'):
                    self.send_json({'login': login, 'eligible': service.allowlist.allows(owner),
                                    'csrfToken': self.csrf(owner), 'operation': service.public(service.get(owner))})
                else:
                    self.send_json({'operation': service.public(service.get(owner))})
            except SignupError as exc:
                self.send_json(exc.public(), exc.status)
            except (ValueError, TypeError):
                self.send_json(SignupError('invalid_request').public(), 400)
            except Exception:
                import sys
                event('controller', 'signup_http_exception', exc_info=sys.exc_info())
                self.send_json(SignupError('internal_error').public(), 500)

    return SignupHandler


def reconcile_forever(service):
    while True:
        try:
            service.tick()
        except Exception:
            import sys
            event('controller', 'signup_reconcile_failed', exc_info=sys.exc_info())
        time.sleep(3)
