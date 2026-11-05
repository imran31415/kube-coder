"""Human decisions are verified and stored by the isolated Keeper service.

Neither the workspace bearer, proxy identity headers nor a local approval file
can mint a grant. Keeper revalidates the browser's GitHub OAuth session. Basic
and development auth bypasses deliberately fail closed until configured.
"""
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
import urllib.parse

from boards.handoff import HandoffError, digest

_contexts = {}
_workers = {}
_lock = threading.Lock()


def issue_context(thread_id):
    token = secrets.token_urlsafe(32)
    with _lock:
        now = time.monotonic()
        for key, value in list(_contexts.items()):
            if value[1] <= now or value[0] == thread_id:
                del _contexts[key]
        if len(_contexts) >= 4096:
            raise RuntimeError('runner context capacity exceeded')
        _contexts[token] = (thread_id, now + 86400)
    return token


def runner_thread(headers):
    token = headers.get('X-KC-Runner-Context', '')
    with _lock:
        value = _contexts.get(token)
        return value[0] if value and value[1] > time.monotonic() else None


def browser_proof(handler):
    if (getattr(handler, '_bearer_only', False) or
            handler.headers.get('Authorization', '').lower().startswith('bearer ')):
        raise HandoffError('human_required', 'Open the dashboard and sign in with GitHub to approve.', 403)
    origin = handler.headers.get('Origin', '')
    if not origin and getattr(handler, 'command', '') == 'GET':
        referer = urllib.parse.urlsplit(handler.headers.get('Referer', ''))
        origin = referer.scheme + '://' + referer.netloc if referer.netloc else ''
    return {'Cookie': handler.headers.get('Cookie', ''), 'Origin': origin}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def authority_call(action, body, proof=None):
    url = os.environ.get('KC_BOARD_APPROVAL_URL', '').rstrip('/')
    token = os.environ.get('KEEPER_CONTROL_SECRET', '')
    if not url or not token:
        raise HandoffError('approval_not_configured',
                           'User approval is not configured for this workspace. Enable GitHub approval in workspace settings.', 503)
    headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}
    headers.update(proof or {})
    request = urllib.request.Request(url + '/' + action, data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=8) as response:
            result = json.loads(response.read(65536))
    except urllib.error.HTTPError as exc:
        messages = {401: ('human_required', 'Sign in with GitHub to approve this request.'),
                    403: ('human_required', 'This action requires your GitHub browser session.'),
                    409: ('decision_conflict', 'This request already has a different decision.'),
                    503: ('approval_not_configured', 'User approval is not configured for this workspace.')}
        code, message = messages.get(exc.code, ('approval_unavailable', 'Could not verify approval. Try again.'))
        raise HandoffError(code, message, exc.code if exc.code in messages else 502,
                           retryable=exc.code >= 500) from exc
    except (OSError, ValueError) as exc:
        raise HandoffError('approval_unavailable', 'Approval service is unavailable. Try again.', 503, retryable=True) from exc
    if not isinstance(result, dict) or not result.get('actor'):
        raise HandoffError('human_required', 'Could not verify your sign-in. Sign in again.', 403)
    return result


def require_human(handler):
    return authority_call('human', {}, browser_proof(handler))['actor']


def decide(record, decision, proof):
    result = authority_call('decision', dict(request_id=record['id'],
        revision=record['revision'], decision_id=decision['decision_id'],
        decision=decision['decision'], expires_at=record['expires_at']), proof)
    return result['actor']


def verify_grant(record):
    if digest(record['scope']) != record['revision']:
        raise HandoffError('approval_mismatch', 'The approved scope no longer matches. Prepare a new request.', 403)
    result = authority_call('grant', dict(request_id=record['id']))
    if any(result.get(key) != value for key, value in {
            'revision': record['revision'], 'decision_id': record['decision_id'],
            'decision': 'approve'}.items()):
        raise HandoffError('approval_mismatch', 'The approved scope no longer matches. Prepare a new request.', 403)
    return result


def enabled():
    return os.environ.get('KC_BOARD_HANDOFF_ENABLED', '').lower() == 'true'


def issue_worker_context(run_id, board_id, item_id):
    token = secrets.token_urlsafe(32)
    with _lock:
        now = time.monotonic()
        for key, value in list(_workers.items()):
            if value['expires'] <= now:
                del _workers[key]
        if len(_workers) >= 4096:
            raise RuntimeError('worker context capacity exceeded')
        _workers[token] = dict(run_id=run_id, board_id=board_id, item_id=item_id, expires=now+86400)
    return token


def worker_context(headers):
    with _lock:
        value = _workers.get(headers.get('X-KC-Board-Worker', ''))
        return dict(value) if value and value['expires'] > time.monotonic() else None
