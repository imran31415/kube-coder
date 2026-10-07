"""Durable, idempotent Build publishing. State lives on the workspace PVC.

The HTTP server supplies task ownership/liveness and event callbacks. Git and
GitHub can be exercised independently against real temporary repositories.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time

import publish_git as pg
from publish_git import PublishError
from publish_github import GitHub
from publish_summary import generate
import worktrees

ACTIVE = {'queued', 'validating', 'committing', 'pushing', 'creating_pr', 'verifying', 'recovering'}
ID = re.compile(r'^[A-Za-z0-9_-]{1,100}$')
_LOCKS = {}
_LOCKS_GATE = threading.Lock()


@contextmanager
def locked(path):
    with _LOCKS_GATE:
        lock = _LOCKS.setdefault(str(path), threading.RLock())
    with lock:
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(path, 'a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def read(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(value, dict) or value.get('version') != 1:
            raise ValueError('schema')
        return value
    except FileNotFoundError:
        return {'version': 1, 'preparation': None, 'operation': None, 'pr': None, 'requests': {}}
    except (OSError, ValueError) as e:
        raise PublishError('recovery_required', 'Publishing state is unreadable. Retained changes have not been discarded.') from e


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.publish-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=True)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Publisher:
    def __init__(self, root, task, assert_idle, *, wt_root=None, github=None,
                 summary=generate, emit=None, checks=None, background=True):
        self.root = Path(root)
        self.task, self.assert_idle = task, assert_idle
        self.wt_root = wt_root
        self.github, self.summary = github or GitHub(), summary
        self.emit = emit or (lambda task, event, data: None)
        self.checks = checks or (lambda meta: {'state': 'unknown', 'source': 'build_log', 'revision_match': False})
        self.background = background
        self._wake = threading.Event()
        self._start_lock = threading.Lock()
        self._thread = None
        self._summary_slots = threading.BoundedSemaphore(1)

    def path(self, task_id):
        if not isinstance(task_id, str) or not ID.fullmatch(task_id):
            raise PublishError('invalid', 'Invalid Build identifier.', 400)
        return self.root / task_id / 'state.json'

    @contextmanager
    def record(self, task_id):
        path = self.path(task_id)
        with locked(str(path) + '.lock'):
            state = read(path)
            yield state
            write(path, state)

    def meta(self, task_id):
        meta = self.task(task_id)
        if not meta:
            raise PublishError('not_found', 'Build not found.', 404)
        return meta

    def ready(self, task_id):
        meta = self.meta(task_id)
        pg.identity(meta)
        self.assert_idle(meta)
        return meta

    def status(self, task_id):
        self.meta(task_id)
        state = read(self.path(task_id))
        error = None
        try:
            self.ready(task_id)
        except PublishError as e:
            error = {'code': e.code, 'message': str(e)}
        # Do not expose private disk/index paths or request bodies to UI.
        prepared = state.get('preparation')
        public = None
        if prepared:
            public = {k: prepared.get(k) for k in ('id', 'fingerprint', 'files', 'title', 'body',
                      'draft', 'draft_revision', 'summary_status', 'summary_error', 'checks', 'author')}
            public['destination'] = {k: v for k, v in prepared['destination'].items() if k != 'push_url'}
        operation = state.get('operation')
        op = {k: operation.get(k) for k in ('id', 'stage', 'commit_sha', 'error', 'created_at', 'updated_at')} if operation else None
        if op:
            op['preparation_id'] = operation['prepared']['id']
        return {'supported': True, 'eligibility': {'can_prepare': error is None,
                'can_publish': error is None and bool(prepared) and state.get('preparing') is None,
                'reason': error}, 'preparation': public, 'operation': op,
                'preparing': state.get('preparing'), 'preparation_error': state.get('preparation_error'),
                'pr': state.get('pr')}

    def prepare(self, task_id, selection=None):
        self.ready(task_id)
        if not self._summary_slots.acquire(blocking=False):
            raise PublishError('busy', 'Another review is being prepared. Try again shortly.', 429)
        prep_id = 'prep-' + secrets.token_hex(12)
        lease = None
        try:
            self.path(task_id).parent.mkdir(parents=True, exist_ok=True)
            lease = open(self.path(task_id).parent / 'preparation.lock', 'a')
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise PublishError('busy', 'This review is already being prepared.') from e
            with self.record(task_id) as state:
                if (state.get('operation') or {}).get('stage') in ACTIVE:
                    raise PublishError('busy', 'Publishing is already in progress.')
                state['preparing'] = prep_id
                state['preparation_error'] = None
        except BaseException:
            if lease:
                lease.close()
            self._summary_slots.release()
            raise
        if self.background:
            threading.Thread(target=self._prepare, args=(task_id, prep_id, selection, lease), daemon=True).start()
        else:
            self._prepare(task_id, prep_id, selection, lease)
        return self.status(task_id)

    def _prepare(self, task_id, prep_id, selection, lease):
        try:
            meta = self.ready(task_id)
            directory = self.path(task_id).parent / prep_id
            destination = self.github.resolve(meta, selection)
            p = pg.snapshot(meta, destination, directory, prep_id)
            again = pg.snapshot(self.ready(task_id), destination, directory)
            if p['fingerprint'] != again['fingerprint']:
                raise PublishError('stale_preparation', 'Files changed during review preparation. Try again.')
            existing = self.github.find(destination)
            if not p['files'] and not existing:
                raise PublishError('no_changes', 'There are no changes to propose against this base.')
            generated = self.summary(p, meta.get('prompt') or '')
            p.update(generated)
            p.update(id=prep_id, draft=False, draft_revision=1, checks=self.checks(meta))
            p['body'] += '\n\n## Testing\n\nCheck results for this exact revision have not been verified. Review the Build log.'
            if p['checks'].get('details'):
                p['body'] += '\n\nBuild log reports (may refer to an earlier revision):\n' + '\n'.join('- ' + s for s in p['checks']['details'])
            with self.record(task_id) as state:
                if state.get('preparing') != prep_id:
                    return
                previous = state.get('preparation') or {}
                # A refresh can finish after a phone saved its description.
                # Preserve saved human edits instead of replacing them with AI.
                if previous.get('draft_revision', 0) > 1:
                    for field in ('title', 'body', 'draft'):
                        p[field] = previous[field]
                    p['draft_revision'] = previous['draft_revision'] + 1
                state['preparation'] = p
                state['preparing'] = None
                if existing:
                    state['pr'] = {'number': existing['number'], 'url': existing['html_url'],
                                   'head_sha': existing['head']['sha'], 'state': existing['state'],
                                   'draft': existing.get('draft', False), 'repo': destination['base_repo']}
                else:
                    state['pr'] = None
                ready_key = self.ready_key(p)
                if state.get('ready_fingerprint') != ready_key:
                    state['ready_fingerprint'] = ready_key
                    state['outbox'] = {'event': 'ready', 'key': 'ready:' + ready_key}
            self.deliver(task_id)
        except Exception as e:
            with self.record(task_id) as state:
                state['preparing'] = None
                state['preparation_error'] = self.error(e)
        finally:
            lease.close()
            self._summary_slots.release()

    @staticmethod
    def error(e):
        return {'code': e.code, 'message': str(e)} if isinstance(e, PublishError) else {
            'code': 'recovery_required', 'message': 'Publishing was interrupted. Retry reconciles saved progress.'}

    def draft(self, task_id, body):
        with self.record(task_id) as state:
            p = self.expected(state, body)
            if (state.get('operation') or {}).get('stage') in ACTIVE:
                raise PublishError('busy', 'This description has already been submitted.')
            for key, limit in (('title', 200), ('body', 20000)):
                value = body.get(key)
                if not isinstance(value, str) or len(value) > limit or (key == 'title' and not value.strip()):
                    raise PublishError('invalid', f'Provide a valid {key} (maximum {limit} characters).', 400)
                p[key] = value.strip()
            p['draft'] = body.get('draft') is True
            p['draft_revision'] += 1
        return self.status(task_id)

    @staticmethod
    def expected(state, body):
        p = state.get('preparation')
        if not p or body.get('preparation_id') != p['id']:
            raise PublishError('stale_preparation', 'Refresh the changes before publishing.')
        if body.get('draft_revision') != p['draft_revision']:
            raise PublishError('draft_conflict', 'The description changed on another device. Your text is preserved; reload the saved draft.')
        return p

    def submit(self, task_id, body):
        key = body.get('idempotency_key')
        if not isinstance(key, str) or not ID.fullmatch(key):
            raise PublishError('invalid', 'A valid idempotency key is required.', 400)
        request_hash = pg.fingerprint(body)
        with self.record(task_id) as state:
            prior = state['requests'].get(key)
            if prior:
                if prior['hash'] != request_hash:
                    raise PublishError('idempotency_conflict', 'This request key was used with different content.')
                return self.status(task_id)
            p = self.expected(state, body)
            if body.get('fingerprint') != p['fingerprint'] or state.get('preparing'):
                raise PublishError('stale_preparation', 'Review the refreshed changes first.')
            current = state.get('operation') or {}
            if current.get('stage') in ACTIVE:
                if current['prepared']['fingerprint'] != p['fingerprint']:
                    raise PublishError('busy', 'Another publish is already in progress.')
            elif current.get('stage') == 'published' and current['prepared']['id'] == p['id']:
                pass
            else:
                if current and current.get('stage') != 'published' and current.get('commit_sha'):
                    raise PublishError('recovery_required', 'Retry the existing operation before starting another publish.')
                now = time.time()
                state['operation'] = {'id': 'pub-' + secrets.token_hex(12), 'stage': 'queued',
                    'prepared': json.loads(json.dumps(p)), 'title': p['title'], 'body': p['body'],
                    'draft': p['draft'], 'created_at': now, 'updated_at': now,
                    'commit_date': datetime.now(timezone.utc).isoformat(), 'error': None}
            state['requests'][key] = {'hash': request_hash, 'operation_id': state['operation']['id']}
            while len(state['requests']) > 256:
                state['requests'].pop(next(iter(state['requests'])))
        self.start()
        self._wake.set()
        return self.status(task_id)

    def retry(self, task_id, operation_id):
        with self.record(task_id) as state:
            op = state.get('operation')
            if not op or op['id'] != operation_id:
                raise PublishError('not_found', 'Publishing operation not found.', 404)
            if op['stage'] not in ACTIVE and op['stage'] != 'published':
                op['stage'], op['error'] = 'recovering', None
        self.start()
        self._wake.set()
        return self.status(task_id)

    def run(self, task_id):
        with locked(self.path(task_id).parent / 'execution.lock'):
            self._run(task_id)

    def _run(self, task_id):
        state = read(self.path(task_id))
        operation = state.get('operation')
        if not operation or operation['stage'] not in ACTIVE:
            # A process can die after persisting its terminal result but before
            # the finally block releases the worktree. Reconcile that lease too.
            if operation and (not operation.get('commit_sha') or operation.get('commit_done')):
                with worktrees.locked(self.wt_root):
                    path = operation['prepared']['path']
                    manifest = worktrees.read_manifest(path)
                    if manifest and manifest.get('publication') == operation['id']:
                        manifest.pop('publication', None)
                        worktrees._write_json(str(Path(path) / worktrees.MANIFEST), manifest)
            return
        p = operation['prepared']
        directory = self.path(task_id).parent / operation['id']
        directory.mkdir(exist_ok=True)

        def save():
            operation['updated_at'] = time.time()
            with self.record(task_id) as state:
                state['operation'] = operation

        manifest_path = Path(p['path']) / worktrees.MANIFEST
        reserved = False
        try:
            with worktrees.locked(self.wt_root):
                meta = self.ready(task_id)
                manifest = worktrees.read_manifest(p['path']) or {}
                reservation = manifest.get('publication')
                if reservation and reservation != operation['id']:
                    raise PublishError('busy', 'Another publisher owns this worktree.')
                manifest['publication'] = operation['id']
                worktrees._write_json(str(manifest_path), manifest)
                reserved = True
            if self.github.identity() != p['destination']['identity']:
                raise PublishError('stale_preparation', 'GitHub identity changed. Restore the reviewed identity to retry.')
            current_dest = self.github.resolve(meta, p['destination'])
            stable = ('head_repo_id', 'head_repo', 'head_branch', 'base_repo_id', 'base_repo', 'base_branch', 'remote', 'identity', 'push_url')
            if any(current_dest.get(k) != p['destination'].get(k) for k in stable):
                raise PublishError('stale_preparation', 'The reviewed destination changed. Restore it before retrying the retained operation.')
            prior_pr = self.github.find(p['destination'])
            if prior_pr and prior_pr['state'] != 'open':
                raise PublishError('pr_closed', 'This branch has a closed or merged PR. Start a new Build branch for new work.')
            if not operation.get('commit_sha'):
                operation['stage'] = 'validating'
                save()
                current = pg.snapshot(meta, current_dest, directory)
                if current['fingerprint'] != p['fingerprint']:
                    raise PublishError('stale_preparation', 'Changes or destination moved since review. No new content was published.')
            operation['stage'] = 'committing'
            save()
            sha = pg.commit(p, operation, save, directory)
            operation['stage'] = 'pushing'
            save()
            pg.push(p, sha, self.github.git_env())
            operation['stage'] = 'creating_pr'
            save()
            pr = self.github.publish(p['destination'], operation)
            operation['stage'], operation['error'] = 'published', None
            with self.record(task_id) as state:
                state['operation'], state['pr'] = operation, pr
                state['outbox'] = {'event': 'published', 'key': operation['id'] + ':published'}
        except Exception as e:
            operation['stage'], operation['error'] = 'failed', self.error(e)
            save()
            with self.record(task_id) as state:
                state['outbox'] = {'event': 'failed', 'key': operation['id'] + ':' + operation['error']['code']}
        finally:
            # Keep ambiguous commit/index transactions reserved until recovery.
            if reserved and (not operation.get('commit_sha') or operation.get('commit_done')):
                with worktrees.locked(self.wt_root):
                    manifest = worktrees.read_manifest(p['path'])
                    if manifest and manifest.get('publication') == operation['id']:
                        manifest.pop('publication', None)
                        worktrees._write_json(str(manifest_path), manifest)
            self.deliver(task_id)

    def deliver(self, task_id):
        state = read(self.path(task_id))
        event = state.get('outbox')
        if not event:
            return
        try:
            self.emit(self.meta(task_id), event, self.status(task_id))
        except Exception:
            return
        with self.record(task_id) as latest:
            if latest.get('outbox') == event:
                latest.pop('outbox', None)

    def start(self):
        if not self.background:
            return
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name='build-publisher', daemon=True)
            self._thread.start()

    def _loop(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / 'worker.lock', 'a') as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            while True:
                for path in self.root.glob('*/state.json'):
                    try:
                        self.probe(path.parent.name)
                        self.recover_preparation(path.parent.name)
                        self.run(path.parent.name)
                        self.deliver(path.parent.name)
                    except Exception:
                        # Corruption of one retained operation cannot break others.
                        continue
                self._wake.wait(3)
                self._wake.clear()

    def recover_preparation(self, task_id):
        # A held OS lock, not an elapsed timeout or reused PID, proves ownership.
        with open(self.path(task_id).parent / 'preparation.lock', 'a') as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            with self.record(task_id) as state:
                if state.get('preparing'):
                    state['preparing'] = None
                    state['preparation_error'] = {'code': 'preparation_interrupted',
                        'message': 'Review preparation was interrupted. Prepare it again; your saved draft is retained.'}

    def reviewed_diff(self, task_id, prep_id, file):
        p = read(self.path(task_id)).get('preparation')
        if not p or p['id'] != prep_id:
            raise PublishError('stale_preparation', 'Refresh the review first.')
        return pg.diff(p, file)

    def queue_probe(self, task_id):
        with self.record(task_id) as state:
            state['probe'] = True
        self._wake.set()

    @staticmethod
    def ready_key(p):
        return pg.fingerprint({k: p[k] for k in ('common', 'branch', 'owner', 'head', 'tree', 'index')})

    def probe(self, task_id):
        state = read(self.path(task_id))
        if not state.get('probe'):
            return
        try:
            meta = self.ready(task_id)
            d = {'base_sha': meta['worktree']['base_sha']}
            p = pg.snapshot(meta, d, self.path(task_id).parent / 'probe')
            with self.record(task_id) as state:
                key = self.ready_key(p)
                if p['files'] and state.get('ready_fingerprint') != key:
                    state['ready_fingerprint'] = key
                    state['outbox'] = {'event': 'ready', 'key': 'ready:' + key}
                state.pop('probe', None)
        except PublishError as e:
            if e.code in ('writer_active', 'liveness_unknown'):
                return  # a stopped CLI may leave an interactive shell alive
            with self.record(task_id) as state:
                state.pop('probe', None)
