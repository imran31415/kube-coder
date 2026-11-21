"""Durable Dex provisioning workflow, independent of HTTP and UI.

Every external effect can be observed/repeated after process death. The worker
advances one persisted stage under the shared Kubernetes Lease per iteration.
"""
import secrets
import time
import uuid
from observability_logging import event
from observability_http import DEADLINE
from observability_metrics import REGISTRY
from provision_store import Conflict
from signup_errors import SignupError

STAGES = ('identity', 'configuration', 'deployment', 'readiness')


class Provisioning:
    def __init__(self, store, dex, backend, domain, allowed_ids):
        self.store, self.dex, self.backend = store, dex, backend
        self.domain, self.allowed_ids = domain, allowed_ids

    def get(self, owner):
        return self.store.get('owner-' + owner)

    def public(self, record):
        if not record:
            return None
        return {key: record.get(key) for key in (
            'operationId', 'slug', 'stage', 'updatedAt', 'startedAt', 'error', 'attempt')} | {
                'url': f'https://{record["slug"]}.{self.domain}/' if record['stage'] == 'ready' else None}

    def create(self, owner, login, admin=False, migrate=False):
        if migrate and not admin:
            raise SignupError('forbidden')
        if not admin and owner not in self.allowed_ids:
            raise SignupError('not_eligible')
        current = self.get(owner)
        if current:
            return current
        info = self.backend.lookup(login)
        if str(info['id']) != owner or info['type'] != 'User':
            raise SignupError('forbidden')
        slug = info['login'].lower()
        if not self.backend.valid_slug(slug):
            raise SignupError('invalid_request')
        # Reserve the slug before checking legacy resources: interrupted attempts
        # can resume, but a different numeric ID can never claim the address.
        reservation = self.store.get('slug-' + slug)
        if reservation and reservation['owner'] != owner:
            raise SignupError('owner_conflict')
        if not reservation:
            if migrate:
                self.backend.validate_migration(slug)
            elif self.backend.occupied(slug):
                raise SignupError('owner_conflict')
            try:
                reservation = self.store.save('slug-' + slug, {'owner': owner, 'migrate': migrate})
            except Conflict:
                reservation = self.store.get('slug-' + slug)
                if reservation['owner'] != owner:
                    raise SignupError('owner_conflict') from None
        # Preserve the authorized mode if a process died after reserving a slug.
        migrate = reservation.get('migrate', False)
        record = {'owner': owner, 'login': info['login'], 'slug': slug,
                  'operationId': uuid.uuid4().hex, 'stage': 'identity', 'attempt': 1,
                  'startedAt': time.time(), 'updatedAt': time.time(), 'error': None, 'migrate': migrate}
        try:
            return self.store.save('owner-' + owner, record)
        except Conflict:
            return self.get(owner)

    def retry(self, owner, admin=False):
        if not admin and owner not in self.allowed_ids:
            raise SignupError('not_eligible')
        record = self.get(owner)
        if not record or record['stage'] not in ('failed', 'deleted'):
            return record
        if record['stage'] == 'deleted':
            record.update(stage='identity', attempt=record['attempt'] + 1,
                          startedAt=time.time(), updatedAt=time.time(), error=None)
            try:
                return self.store.save('owner-' + owner, record)
            except Conflict:
                return self.get(owner)
        if not record.get('error', {}).get('retryable'):
            raise SignupError('identity_conflict')
        if time.time() - record['updatedAt'] < 15:
            raise SignupError('busy')
        record['stage'] = record['failedStage']
        if record['stage'] in ('deployment', 'readiness'):
            # Do not replace an in-flight Job just because observation failed.
            if self.backend.job_failed(record):
                record['attempt'] += 1
                record['stage'] = 'deployment'
            elif not self.backend.job(record):
                record['stage'] = 'deployment'
        record.update(error=None, startedAt=time.time(), updatedAt=time.time())
        try:
            return self.store.save('owner-' + owner, record)
        except Conflict:
            return self.get(owner)

    def tick(self):
        with self.store.leader() as acquired:
            if not acquired:
                return
            records = self.store.operations()
            records.sort(key=lambda item: item[1]['updatedAt'])
            for name, record in records:
                if record['stage'] == 'ready' and time.time() - record['updatedAt'] > 60:
                    # Observe namespace deletion twice. API failures propagate,
                    # never becoming a destructive absence signal.
                    if self.backend.deleted(record['slug']):
                        if record.get('missingSince') and time.time() - record['missingSince'] >= 60:
                            self.dex.delete('workspace-' + record['owner'])
                            record['stage'] = 'deleted'
                        else:
                            record['missingSince'] = time.time()
                    else:
                        record.pop('missingSince', None)
                    record['updatedAt'] = time.time()
                    self.store.save(name, record)
                    return
                if record['stage'] not in STAGES:
                    continue
                self.advance(name, record)
                return

    def advance(self, name, record):
        with REGISTRY.operation('signup_' + record['stage']) as result:
            self._advance(name, record)
            if record['stage'] == 'failed':
                result['outcome'] = 'error'

    def _advance(self, name, record):
        stage = record['stage']
        started = time.monotonic()
        deadline = DEADLINE.set(started + 180)
        try:
            credentials = self.store.credentials(record['owner'], lambda: {
                'client-secret': secrets.token_urlsafe(32),
                'cookie-secret': secrets.token_urlsafe(24)})
            client_id = 'workspace-' + record['owner']
            if stage == 'identity':
                self.dex.ensure(client_id, credentials['client-secret'],
                    f'https://{record["slug"]}.{self.domain}/oauth2/callback')
                record['stage'] = 'configuration'
            elif stage == 'configuration':
                record['gitCommit'] = self.backend.publish(record, client_id, credentials)
                record['stage'] = 'deployment'
            elif stage == 'deployment':
                self.backend.deploy(record)
                record['stage'] = 'readiness'
            elif stage == 'readiness':
                if self.backend.ready(record):
                    record['stage'] = 'ready'
                elif time.time() - record['startedAt'] > 1200:
                    raise SignupError('readiness_timeout')
            record['error'] = None
        except SignupError as exc:
            record.update(stage='failed', failedStage=stage,
                          error=exc.public(operation_id=record['operationId'], stage=stage))
        except Exception:
            # Unexpected exceptions are logged with stack locations, not values.
            import sys
            event('controller', 'signup_exception', exc_info=sys.exc_info(),
                  operation_id=record['operationId'], stage=stage)
            record.update(stage='failed', failedStage=stage,
                          error=SignupError('internal_error').public())
        finally:
            DEADLINE.reset(deadline)
        record['updatedAt'] = time.time()
        self.store.save(name, record)
        event('controller', 'signup_stage', operation_id=record['operationId'],
              workspace=record['slug'], stage=stage, result=record['stage'],
              duration_ms=round((time.monotonic() - started) * 1000))

    def cleanup(self, slug):
        with self.store.leader() as acquired:
            if not acquired:
                raise SignupError('busy')
            reservation = self.store.get('slug-' + slug)
            if not reservation:
                raise SignupError('owner_conflict')
            owner = reservation['owner']
            record = self.get(owner)
            if not record or record['stage'] not in ('ready', 'deleted'):
                raise SignupError('owner_conflict')
            if not self.backend.absent(slug):
                raise SignupError('owner_conflict')
            self.dex.delete('workspace-' + owner)
            record.update(stage='deleted', updatedAt=time.time())
            self.store.save('owner-' + owner, record)
            return {'ok': True}
