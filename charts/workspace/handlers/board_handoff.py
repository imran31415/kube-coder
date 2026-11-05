"""HTTP adapter and injected local workspace dependencies for Board handoff."""
import json
import logging
import os
import re
import threading
import urllib.parse

import approval_auth
import handlers
from boards.handoff import HandoffError, HandoffManager, digest
from handlers.routing import RouteTable

LOG = logging.getLogger('board.handoff')
_manager = None
_lock = threading.Lock()


def repository_identity(value):
    if not isinstance(value, str) or not value.strip():
        raise HandoffError('repository_required', 'Choose a repository from Projects.')
    value = value.strip()
    if re.fullmatch(r'[^/@:]+/[^/@:]+', value):
        value = 'https://github.com/' + value
    if value.startswith('git@'):
        value = 'ssh://' + value.replace(':', '/', 1)
    parsed = urllib.parse.urlsplit(value)
    parts = parsed.path.strip('/').removesuffix('.git').split('/')
    if (parsed.scheme not in ('https', 'ssh') or not parsed.hostname or len(parts) != 2
            or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', p) for p in parts)
            or parsed.query or parsed.fragment or parsed.password):
        raise HandoffError('invalid_repository', 'Choose a valid repository URL.')
    return 'https://' + parsed.hostname.lower() + '/' + '/'.join(parts).lower()


def resolve(data):
    server = handlers.server
    board_id = data.get('board_id')
    if not isinstance(board_id, str):
        raise HandoffError('board_required', 'Choose a connected Board before requesting a run.')
    cfg = server.BoardsManager.get(board_id)
    if not cfg:
        raise HandoffError('board_not_found', 'Board not found. Choose or connect a Board.', 404)
    repository = repository_identity(data.get('repository'))
    candidates = []
    for project in server.ProjectsManager._load_all():
        try:
            if repository_identity(project.get('repo')) == repository:
                candidates.append(project)
        except HandoffError:
            continue
    workdir = (data.get('options') or {}).get('workdir')
    if workdir and not any(workdir in (p.get('workdirs') or []) for p in candidates):
        raise HandoffError('repository_mismatch', 'The checkout does not belong to the selected repository.')
    if not candidates:
        raise HandoffError('repository_not_found', 'Add this repository to Projects before running its Board.', 404)
    return dict(board_id=board_id, board_name=cfg.get('display_name') or board_id,
                workspace_id=os.environ.get('WORKSPACE_USER', 'local'),
                repository=repository, config_digest=digest(cfg),
                project_digest=digest([dict(id=p['id'], repo=p.get('repo'),
                                           workdirs=p.get('workdirs')) for p in candidates]))


def execute(record):
    server = handlers.server
    approval_auth.verify_grant(record)
    scope = record['scope']
    fresh = resolve(dict(board_id=scope['board_id'], repository=scope['repository'], options=scope['options']))
    if digest(scope) != record['revision'] or any(scope.get(k) != v for k, v in fresh.items()):
        raise HandoffError('scope_changed', 'Board or repository changed. Prepare a new request.', 409)
    if scope['operation'] == 'stop':
        if not server.BoardRunsManager.request_stop(scope['target_run_id']):
            raise HandoffError('run_finished', 'This run has already finished.', 409)
        return
    cfg = server.BoardsManager.get(scope['board_id'])
    if not cfg or digest(cfg) != scope['config_digest']:
        raise HandoffError('scope_changed', 'Board settings changed. Prepare a new request.', 409)
    run, error = server.BoardRunsManager.create(cfg, scope['options'], origin='chat',
        reserved_run_id=record['run_id'], origin_request_id=record['id'],
        origin_thread_id=record['thread_id'])
    if error:
        code = getattr(error, 'code', 'run_start_refused')
        messages = {
            'board_busy': 'This Board already has a run. Open Board to check its progress.',
            'worker_capacity': 'All workers are busy. Wait for a task to finish, then prepare a new request.',
            'worktree_capacity': 'No isolated checkout slots are available. Clean up old worktrees in Board.',
            'repository_unavailable': 'The repository checkout is unavailable. Check its path and base revision.',
            'workspace_auth_unavailable': 'Worker authentication needs repair. Open workspace settings.',
            'board_fetch_failed': 'Could not reach the Board. Check its connection and credentials.',
            'board_listing_failed': 'Could not read Board items. Check credentials and selection settings.',
        }
        LOG.warning('board startup refused request_id=%s run_id=%s code=%s', record['id'], record['run_id'], code)
        raise HandoffError(code, messages.get(code, 'Board could not start. Check the run options and prepare a new request.'),
                           getattr(error, 'status', 409), retryable=getattr(error, 'retryable', False))
    # One existing watcher, local state only. Failure must not undo a started run.
    try:
        session = server.HypervisorSession.get(record['thread_id'])
        meta = session.read_meta() if session else None
        if (meta and not meta.get('deleted_at') and server.hv_watchers and
                meta.get('board_watch_epoch', 0) == scope.get('watch_epoch', 0)):
            server.hv_watchers.arm(record['thread_id'], 'board_run', run['id'], timeout=86400)
    except Exception:
        LOG.exception('board completion watcher unavailable request_id=%s', record['id'])


def manager():
    global _manager
    with _lock:
        if _manager is None:
            server = handlers.server
            _manager = HandoffManager(os.path.join(server.BoardsManager.boards_dir(), 'requests'),
                resolve=resolve, execute=execute, get_run=server.BoardRunsManager.get,
                publish=server.EventBroker.publish, authority=approval_auth.decide)
        return _manager


def public_request(record):
    """Review counts are local; no connector calls on a status refresh."""
    record = dict(record)
    if record.get('run_id'):
        records = handlers.server.BoardReviewManager.list_records(record['board_id'], open_only=True)
        record['review_needed'] = sum(1 for r in records if r.get('run_id') == record['run_id'])
    return record


class BoardHandoffRoutes:
    def _handoff(self, action, request_id=None):
        try:
            if not self._board_guard():
                return
            if not approval_auth.enabled():
                raise HandoffError('approval_not_configured', 'User approval is not configured for this workspace.', 503)
            action()
        except HandoffError as exc:
            self.send_json(exc.body(request_id), exc.status)
        except (ValueError, TypeError):
            self.send_json(HandoffError('invalid_request', 'Invalid request body.').body(request_id), 400)
        except Exception:
            LOG.exception('board request failed request_id=%s', request_id)
            self.send_json(HandoffError('request_unavailable', 'Could not save or read the Board request. Try again.',
                                       503, retryable=True).body(request_id), 503)

    def handle_board_request_prepare(self):
        def prepare():
            data = self.read_json_body(max_bytes=16384)
            thread = approval_auth.runner_thread(self.headers)
            if not thread:
                approval_auth.require_human(self)
                if not isinstance(data, dict):
                    raise HandoffError('invalid_request', 'Invalid request body.')
                thread = data.pop('thread_id', None)
            elif isinstance(data, dict) and 'thread_id' in data:
                raise HandoffError('thread_mismatch', 'The conversation is supplied by the authenticated runner.', 403)
            session = handlers.server.HypervisorSession.get(thread) if isinstance(thread, str) else None
            if session is None or (session.read_meta() or {}).get('deleted_at'):
                raise HandoffError('thread_not_found', 'Conversation not found.', 404)
            self.send_json(manager().prepare(data, thread_id=thread,
                watch_epoch=(session.read_meta() or {}).get('board_watch_epoch', 0)), 201)
        self._handoff(prepare)

    def handle_board_request_get(self, request_id):
        def get():
            record = manager().get(request_id)
            thread = approval_auth.runner_thread(self.headers)
            if thread and record['thread_id'] != thread:
                raise HandoffError('thread_mismatch', 'This request belongs to another conversation.', 403)
            self.send_json(public_request(record))
        self._handoff(get, request_id)

    def handle_board_request_list(self, thread_id):
        if not approval_auth.enabled():
            if self._board_guard():
                self.send_json({'requests': [], 'enabled': False})
            return
        def listing():
            thread = approval_auth.runner_thread(self.headers)
            if thread and thread != thread_id:
                raise HandoffError('thread_mismatch', 'This request belongs to another conversation.', 403)
            if handlers.server.HypervisorSession.get(thread_id) is None:
                raise HandoffError('thread_not_found', 'Conversation not found.', 404)
            self.send_json({'requests': [public_request(r) for r in manager().list(thread_id)]})
        self._handoff(listing)

    def handle_board_request_decision(self, request_id):
        def decide():
            result = manager().decide(request_id, self.read_json_body(max_bytes=4096), human=approval_auth.browser_proof(self))
            self.send_json(result, 202 if result['state'] == 'starting' else 200)
        self._handoff(decide, request_id)


ROUTES = RouteTable()
ROUTES.add('POST', '/api/boards/run-requests', 'handle_board_request_prepare')
ROUTES.add('GET', re.compile(r'^/api/boards/run-requests/(brq-[a-f0-9]{32})$'), 'handle_board_request_get')
ROUTES.add('POST', re.compile(r'^/api/boards/run-requests/(brq-[a-f0-9]{32})/decision$'), 'handle_board_request_decision')
ROUTES.add('GET', re.compile(r'^/api/hypervisor/threads/([A-Za-z0-9_-]+)/board-requests$'), 'handle_board_request_list')
