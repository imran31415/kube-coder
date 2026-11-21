"""Small Kubernetes state adapter. Mutations use optimistic concurrency.

Only this module knows Kubernetes wire objects. A dedicated namespace contains
operation ConfigMaps, immutable credential Secrets and a reconciliation Lease.
"""
import base64
import contextlib
import datetime
import json
import logging
import os
import ssl
import urllib.error
import urllib.request
import uuid
from signup_errors import SignupError
from observability_logging import event


class Conflict(Exception):
    pass


EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def micro_time(value):
    """Kubernetes MicroTime (Lease renewTime/acquireTime) wire format.

    The API server parses it with Go's RFC3339Micro layout, which REQUIRES six
    fractional digits: '...00Z' is rejected with a 400. isoformat() also omits
    the fraction whenever microsecond == 0, so always format explicitly.
    """
    return value.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


class KubeStore:
    def __init__(self, namespace):
        self.namespace = namespace
        self.identity = uuid.uuid4().hex
        host = os.environ.get('KUBERNETES_SERVICE_HOST', 'kubernetes.default.svc')
        if ':' in host and not host.startswith('['):
            host = '[' + host + ']'
        self.root = 'https://' + host + ':' + os.environ.get('KUBERNETES_SERVICE_PORT', '443')
        self.sa = '/var/run/secrets/kubernetes.io/serviceaccount/'

    def request(self, method, path, body=None, missing=False):
        try:
            with open(self.sa + 'token') as f:
                token = f.read().strip()
            req = urllib.request.Request(self.root + path, method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, context=ssl.create_default_context(cafile=self.sa + 'ca.crt'), timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and missing:
                return None
            if exc.code == 409:
                raise Conflict() from None
            event('controller', 'signup_state_failed', logging.WARNING, method=method, path=path, upstream_status=exc.code)
            raise SignupError('state_unavailable') from None
        except (OSError, ValueError) as exc:
            event('controller', 'signup_state_failed', logging.WARNING, method=method, path=path,
                  exception_type=type(exc).__name__)
            raise SignupError('state_unavailable') from None

    def path(self, resource, name=''):
        api = '/apis/coordination.k8s.io/v1' if resource == 'leases' else '/api/v1'
        return f'{api}/namespaces/{self.namespace}/{resource}' + ('/' + name if name else '')

    def get(self, name):
        obj = self.request('GET', self.path('configmaps', name), missing=True)
        if obj is None:
            return None
        return dict(json.loads(obj['data']['record']), _version=obj['metadata']['resourceVersion'])

    def save(self, name, record):
        data = {k: v for k, v in record.items() if not k.startswith('_')}
        obj = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': name,
            'labels': {'kube-coder.dev/signup': 'true'}}, 'data': {'record': json.dumps(data)}}
        if record.get('_version'):
            obj['metadata']['resourceVersion'] = record['_version']
        result = self.request('PUT' if record.get('_version') else 'POST',
            self.path('configmaps', name if record.get('_version') else ''), obj)
        return dict(data, _version=result['metadata']['resourceVersion'])

    def operations(self):
        result = self.request('GET', self.path('configmaps') + '?labelSelector=kube-coder.dev%2Fsignup%3Dtrue')
        return [(o['metadata']['name'], dict(json.loads(o['data']['record']),
            _version=o['metadata']['resourceVersion'])) for o in result['items']
            if o['metadata']['name'].startswith('owner-')]

    def credentials(self, owner, factory):
        name = 'identity-' + owner
        obj = self.request('GET', self.path('secrets', name), missing=True)
        if obj is None:
            value = factory()
            obj = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name},
                   'immutable': True, 'type': 'Opaque', 'data': {
                       k: base64.b64encode(v.encode()).decode() for k, v in value.items()}}
            try:
                obj = self.request('POST', self.path('secrets'), obj)
            except Conflict:
                obj = self.request('GET', self.path('secrets', name))
        return {k: base64.b64decode(v).decode() for k, v in obj['data'].items()}

    @contextlib.contextmanager
    def leader(self):
        """One short reconciliation step per lease; steps have bounded deadlines.

        Do not steal a live lease. A crashed worker can resume after expiry.
        The lease exceeds the maximum bounded Git operation (clone/commit/push).
        External writes must also be idempotent because leases cannot fence Dex.
        """
        name = 'signup-reconcile'
        path = self.path('leases', name)
        obj = self.request('GET', path, missing=True)
        now = datetime.datetime.now(datetime.timezone.utc)
        if obj:
            spec = obj.get('spec', {})
            renew = datetime.datetime.fromisoformat(spec['renewTime'].replace('Z', '+00:00'))
            if (now - renew).total_seconds() < spec['leaseDurationSeconds']:
                yield False
                return
        lease = {'apiVersion': 'coordination.k8s.io/v1', 'kind': 'Lease',
                 'metadata': {'name': name}, 'spec': {'holderIdentity': self.identity,
                 'leaseDurationSeconds': 600, 'renewTime': micro_time(now)}}
        if obj:
            lease['metadata']['resourceVersion'] = obj['metadata']['resourceVersion']
        try:
            held = self.request('PUT' if obj else 'POST', path if obj else self.path('leases'), lease)
        except Conflict:
            yield False
            return
        try:
            yield True
        finally:
            # CAS prevents releasing a lease acquired by a replacement worker.
            held['spec']['leaseDurationSeconds'] = 1
            held['spec']['renewTime'] = micro_time(EPOCH)
            with contextlib.suppress(Conflict, SignupError):
                self.request('PUT', path, held)
