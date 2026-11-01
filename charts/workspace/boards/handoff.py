"""Durable approval requests. Dependencies perform local reads unless named execute.

An approval request is not a run. In particular, prepare never calls the runner,
connector, credential provider or worktree manager. The isolated authority owns
human decisions; workspace JSON is only a recoverable view of those decisions.
"""
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time

from . import runs
from .store import JsonRecord

LOG = logging.getLogger('board.handoff')
if not LOG.handlers:
    LOG.addHandler(logging.StreamHandler())
LOG.setLevel(logging.INFO)
LOG.propagate = False
REQUEST_ID = re.compile(r'^brq-[a-f0-9]{32}$')
TTL = 900
MAX_HISTORY = 100
MAX_RECORDS = 1000
RETENTION = 30 * 86400


class HandoffError(Exception):
    def __init__(self, code, message, status=400, *, retryable=False):
        super().__init__(message)
        self.code, self.status, self.retryable = code, status, retryable

    def body(self, request_id=None):
        return dict(error=str(self), code=self.code, retryable=self.retryable,
                    request_id=request_id)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode()).hexdigest()


def options(data):
    if not isinstance(data, dict):
        raise HandoffError('invalid_options', 'Run options must be an object.')
    unknown = set(data) - {'select', 'concurrency', 'workdir', 'isolate', 'base_ref', 'mode'}
    if unknown:
        raise HandoffError('invalid_options', 'Unknown run option: ' + sorted(unknown)[0])
    selection = data.get('select', {'limit': 3})
    if isinstance(selection, dict):
        selection = {'limit': 3, **selection}
    if isinstance(selection, dict) and ('ignore_processed' in selection or
                                        isinstance(selection.get('limit'), bool)):
        raise HandoffError('invalid_selection', 'Choose a valid item limit and selection.')
    select, errors = runs.validate_select(selection)
    if errors:
        raise HandoffError('invalid_selection', '; '.join(errors[:3]))
    concurrency = data.get('concurrency', 1)
    if type(concurrency) is not int or not 1 <= concurrency <= 8:
        raise HandoffError('invalid_concurrency', 'Choose between 1 and 8 workers.')
    if data.get('mode', 'propose') != 'propose':
        raise HandoffError('invalid_mode', 'Chat runs require Propose mode.')
    repo, errors = runs.validate_repo_config({**data, 'isolate': data.get('isolate', True)})
    if errors:
        raise HandoffError('invalid_repository', '; '.join(errors[:3]))
    if not repo['workdir'] or not repo['isolate']:
        raise HandoffError('invalid_repository', 'Choose a repository with an isolated worktree.')
    return dict(select=select, concurrency=concurrency, mode='propose', **repo)


class HandoffManager:
    def __init__(self, directory, *, resolve, execute, get_run, publish,
                 authority, clock=time.time):
        self.directory = directory
        self.resolve, self.execute, self.get_run = resolve, execute, get_run
        self.publish, self.authority, self.clock = publish, authority, clock
        self._slots = threading.BoundedSemaphore(1)

    def _record(self, request_id):
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise HandoffError('not_found', 'Board request not found.', 404)
        return JsonRecord(os.path.join(self.directory, request_id + '.json'))

    def _emit(self, record):
        LOG.info(json.dumps(dict(event='board.request', timestamp=self.clock(), severity='INFO',
                                elapsed_ms=round(max(0, self.clock()-record['created_at'])*1000), request_id=record['id'],
                                thread_id=record['thread_id'], board_id=record['board_id'],
                                run_id=record.get('run_id'), state=record['state'],
                                code=record.get('error', {}).get('code'))))
        try:
            self.publish('boards.request', dict(id=record['id'], thread_id=record['thread_id']))
        except Exception:
            LOG.exception('board request event delivery failed: %s', record['id'])

    def prepare(self, data, *, thread_id, watch_epoch=0):
        if not isinstance(data, dict) or set(data) - {'board_id', 'repository', 'options', 'operation', 'run_id'}:
            raise HandoffError('invalid_request', 'Choose a Board, repository and run options.')
        if not isinstance(thread_id, str) or not thread_id:
            raise HandoffError('thread_required', 'Open a conversation to request a Board run.', 403)
        operation = data.get('operation', 'start')
        if operation not in ('start', 'stop'):
            raise HandoffError('invalid_operation', 'Choose start or stop.')
        # resolve is restricted to stored configuration/project metadata.
        cleaned = options(data.get('options', {})) if operation == 'start' else {}
        target = self.resolve(dict(data, options=cleaned))
        if operation == 'stop':
            run = self.get_run(data.get('run_id', ''))
            if not run or run.get('board_id') != target['board_id']:
                raise HandoffError('not_found', 'Board run not found.', 404)
            # Show the actual run checkout; resolve verifies its registered repository.
            cleaned = {key: run.get(key) for key in ('select', 'concurrency', 'workdir', 'base_ref', 'isolate', 'mode')}
            target = self.resolve(dict(data, options=cleaned))
        self.prune()
        request_id = 'brq-' + secrets.token_hex(16)
        now = self.clock()
        scope = dict(**target, options=cleaned, operation=operation,
                     thread_id=thread_id, watch_epoch=watch_epoch,
                     target_run_id=data.get('run_id') if operation == 'stop' else None)
        record = dict(id=request_id, thread_id=thread_id, scope=scope,
                      board_id=target['board_id'], revision=digest(scope),
                      created_at=now, expires_at=now + TTL, state='pending',
                      decision_id=None, actor=None, run_id=None, error={}, version=1)
        self._record(request_id).write(record)
        self._emit(record)
        return record

    def get(self, request_id):
        store = self._record(request_id)
        record = store.read()
        if not record or record.get('id') != request_id or 'scope' not in record:
            raise HandoffError('not_found', 'Board request not found.', 404)
        if record['state'] == 'pending' and record['expires_at'] <= self.clock():
            def expire(current):
                if current.get('state') != 'pending':
                    return False
                current['state'] = 'expired'
                current['version'] = current.get('version', 1) + 1
            record, changed = store.update(expire)
            if changed:
                self._emit(record)
        if record.get('run_id'):
            run = self.get_run(record['run_id'])
            record['run'] = runs.summary(run) if run else None
        return record

    def list(self, thread_id, *, before=None):
        try:
            names = os.listdir(self.directory)
        except FileNotFoundError:
            return []
        records = []
        for name in names:
            if not name.endswith('.json') or not REQUEST_ID.fullmatch(name[:-5]):
                continue
            try:
                record = self.get(name[:-5])
            except HandoffError:
                continue
            if record['thread_id'] == thread_id and (before is None or record['created_at'] < before):
                records.append(record)
        return sorted(records, key=lambda r: r['created_at'], reverse=True)[:MAX_HISTORY]

    def decide(self, request_id, data, *, human):
        if (not isinstance(data, dict) or set(data) != {'decision', 'revision', 'decision_id'}
                or data['decision'] not in ('approve', 'reject')
                or not isinstance(data['decision_id'], str)
                or not re.fullmatch(r'[a-zA-Z0-9-]{16,80}', data['decision_id'])):
            raise HandoffError('invalid_decision', 'Invalid approval decision.')
        original = self.get(request_id)
        if original['revision'] != data['revision'] or digest(original['scope']) != data['revision']:
            raise HandoffError('scope_changed', 'The request changed. Review a new request.', 409)
        if original['decision_id']:
            if original['decision_id'] != data['decision_id'] or original.get('decision') != data['decision']:
                raise HandoffError('already_decided', 'This request already has a decision.', 409)
            # Replays still require an authenticated human, checked by the authority.
            self.authority(original, data, human)
            return original
        if original['state'] != 'pending':
            raise HandoffError('request_expired', 'This request expired. Prepare a new one.', 409)
        scope = original['scope']
        fresh = self.resolve(dict(board_id=scope['board_id'], repository=scope['repository'],
                                  options=scope['options']))
        if any(fresh.get(k) != scope.get(k) for k in fresh):
            raise HandoffError('scope_changed', 'Board or repository settings changed. Review a new request.', 409)
        approved = data['decision'] == 'approve'
        if approved and not self._slots.acquire(blocking=False):
            raise HandoffError('startup_busy', 'Another Board run is starting. Try again shortly.', 429, retryable=True)
        try:
            # The authority persists an immutable decision outside agent-writable storage.
            actor = self.authority(original, data, human)
            def claim(record):
                if record.get('state') != 'pending' or record.get('decision_id'):
                    if record.get('decision_id') == data['decision_id'] and record.get('decision') == data['decision']:
                        return False
                    raise HandoffError('already_decided', 'This request already has a decision.', 409)
                if record['expires_at'] <= self.clock():
                    raise HandoffError('request_expired', 'This request expired. Prepare a new one.', 409)
                record.update(decision_id=data['decision_id'], decision=data['decision'],
                              version=record.get('version', 1) + 1,
                              actor=actor, approved_at=self.clock(),
                              state='starting' if approved else 'rejected',
                              run_id=(scope['target_run_id'] if scope['operation'] == 'stop'
                                      else runs.make_run_id(self.clock(), secrets.token_hex(4))) if approved else None)
            record, changed = self._record(request_id).update(claim)
            if changed:
                self._emit(record)
            if approved and changed:
                threading.Thread(target=self._start, args=(record,), daemon=True,
                                 name='board-handoff-start').start()
            elif approved:
                self._slots.release()
            return record
        except BaseException:
            if approved:
                self._slots.release()
            raise

    def _start(self, record):
        try:
            self.execute(record)
            self._settle(record['id'], 'started')
        except HandoffError as exc:
            self._settle(record['id'], 'failed', exc.body(record['id']))
        except Exception:
            LOG.exception('board startup failed request_id=%s run_id=%s', record['id'], record['run_id'])
            self._settle(record['id'], 'interrupted', dict(code='startup_uncertain',
                         error='Could not confirm startup. Open Board before trying again.', retryable=False))
        finally:
            self._slots.release()

    def _settle(self, request_id, state, error=None):
        def settle(record):
            if record.get('state') != 'starting':
                return False
            record.update(state=state, error=error or {}, version=record.get('version', 1) + 1)
        record, changed = self._record(request_id).update(settle)
        if changed:
            self._emit(record)

    def recover(self):
        """Called once at boot after the existing runner reconciles its runs."""
        try:
            names = os.listdir(self.directory)
        except FileNotFoundError:
            return
        for name in names:
            if name.endswith('.json') and REQUEST_ID.fullmatch(name[:-5]):
                record = self._record(name[:-5]).read()
                if record.get('state') == 'starting':
                    run = self.get_run(record.get('run_id', ''))
                    linked = run and run.get('origin_request_id') == record['id']
                    self._settle(record['id'], 'started' if linked else 'interrupted',
                                 None if linked else dict(code='startup_interrupted',
                                     error='Startup was interrupted. Check Board before making a new request.'))

    def prune(self):
        """Bound disk/history growth without removing live or recent replay evidence."""
        try:
            names = [n for n in os.listdir(self.directory)
                     if n.endswith('.json') and REQUEST_ID.fullmatch(n[:-5])]
        except FileNotFoundError:
            return
        retained = 0
        for name in names:
            record = self.get(name[:-5])
            run = record.get('run')
            if (record['state'] not in ('pending', 'starting') and
                    (not run or run.get('status') != 'running') and
                    record['created_at'] < self.clock() - RETENTION):
                self._record(record['id']).delete()
            else:
                retained += 1
        if retained >= MAX_RECORDS:
            raise HandoffError('request_capacity', 'Board request history is full. Ask your workspace administrator to archive older requests.', 429)
