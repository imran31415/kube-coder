"""Bridge the signup state machine to the existing GitOps/Job implementation."""
import json
import subprocess
from signup_errors import SignupError
from observability_logging import event


class Backend:
    def __init__(self, controller, issuer, shared_tls):
        self.c, self.issuer, self.shared_tls = controller, issuer, shared_tls

    def lookup(self, login):
        if not self.valid_slug(login):
            raise SignupError('invalid_request')
        try:
            return self.c._github_api('GET', '/users/' + login, token=self.c.GITOPS_TOKEN)
        except self.c.GithubError:
            raise SignupError('state_unavailable') from None

    def valid_slug(self, slug):
        return bool(self.c._GH_LOGIN_RE.fullmatch(slug))

    def resource(self, kind, name, namespace):
        # Kubernetes --ignore-not-found distinguishes absence from RBAC/network
        # failures. The legacy workspace_exists helper does not; never use it.
        proc = subprocess.run(['kubectl', 'get', kind, name, '-n', namespace,
            '--ignore-not-found', '-o', 'json'], capture_output=True, text=True, timeout=15)
        if proc.returncode:
            event('controller', 'signup_resource_failed', resource=kind, resource_name=name,
                  namespace=namespace, return_code=proc.returncode)
            raise SignupError('state_unavailable')
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def absent(self, slug):
        for ns in (self.c.ns_for_user(slug), self.c.NAMESPACE):
            # RBAC is checked before resource existence. A new tenant namespace
            # has no RoleBinding yet, so first use the namespace discovery grant.
            if self.resource('namespace', ns, self.c.NAMESPACE) is None:
                continue
            if self.resource('deployment', self.c.WORKSPACE_PREFIX + slug, ns):
                return False
        return True

    def occupied(self, slug):
        if not self.absent(slug):
            return True
        path = f'/repos/{self.c._gitops_owner_repo()}/contents/users-private/{slug}/values.yaml?ref={self.c.GITOPS_BRANCH}'
        try:
            self.c._github_api('GET', path, token=self.c.GITOPS_TOKEN)
            return True
        except self.c.GithubError as exc:
            if exc.status == 404:
                return False
            raise SignupError('state_unavailable') from None

    def deleted(self, slug):
        return (self.resource('namespace', self.c.ns_for_user(slug), self.c.NAMESPACE) is None
                and self.absent(slug))

    def publish(self, record, client_id, credentials):
        opts = {'login': record['login'], 'slug': record['slug'],
                'host': record['slug'] + '.' + self.c.WORKSPACE_DOMAIN,
                'ownerGithubId': record['owner'], 'oidcIssuer': self.issuer,
                'sharedTls': self.shared_tls, 'migrate': record.get('migrate', False)}
        # Each retry re-clones the branch, incorporating unrelated remote commits.
        for attempt in range(3):
            try:
                return self.c.gitops_publish(opts, client_id, credentials['client-secret'], credentials['cookie-secret'])
            except (self.c.ProvisionError, subprocess.TimeoutExpired) as exc:
                event('controller', 'signup_gitops_retry', operation_id=record['operationId'],
                      attempt=attempt + 1, exception_type=type(exc).__name__)
                if attempt == 2:
                    raise SignupError('gitops_failed') from None

    def validate_migration(self, slug):
        # Keep migration explicit and restricted to the same standard host.
        # Custom hostnames need an operator-reviewed GitOps migration instead.
        try:
            workspace = self.c.find_workspace(slug)
        except (LookupError, ValueError):
            raise SignupError('owner_conflict') from None
        expected = f'https://{slug}.{self.c.WORKSPACE_DOMAIN}'
        if (not workspace or workspace.get('url', '').rstrip('/') != expected
                or workspace.get('namespace') != self.c.ns_for_user(slug)):
            raise SignupError('owner_conflict')

    @staticmethod
    def job_name(record):
        return 'signup-' + record['operationId'] + '-' + str(record['attempt'])

    def job(self, record):
        return self.resource('job', self.job_name(record), self.c.NAMESPACE)

    def deploy(self, record):
        existing = self.job(record)
        if existing:
            if existing['metadata'].get('labels', {}).get('signup-operation') != record['operationId']:
                raise SignupError('identity_conflict')
            return
        manifest = self.c.build_job_manifest(record['slug'])
        manifest['metadata']['name'] = self.job_name(record)
        manifest['metadata']['labels']['signup-operation'] = record['operationId']
        manifest['spec']['template']['spec']['containers'][0]['env'].append(
            {'name': 'GITOPS_COMMIT', 'value': record['gitCommit']})
        # Keep Job evidence until cleanup; the worker may restart after >1 hour.
        manifest['spec'].pop('ttlSecondsAfterFinished', None)
        try:
            self.c._kubectl_apply(manifest)
        except self.c.KubectlError:
            raise SignupError('deploy_failed') from None

    def job_failed(self, record):
        job = self.job(record)
        return bool(job and any(c.get('type') == 'Failed' and c.get('status') == 'True'
            for c in job.get('status', {}).get('conditions', [])))

    def ready(self, record):
        job = self.job(record)
        if not job:
            raise SignupError('deploy_failed')
        if self.job_failed(record):
            raise SignupError('deploy_failed')
        if not job.get('status', {}).get('succeeded'):
            return False
        ns = self.c.ns_for_user(record['slug'])
        for name in (self.c.WORKSPACE_PREFIX + record['slug'], 'oauth2-proxy-' + record['slug']):
            deployment = self.resource('deployment', name, ns)
            if not deployment:
                return False
            status = deployment.get('status', {})
            desired = deployment.get('spec', {}).get('replicas', 1)
            if (desired < 1 or status.get('observedGeneration', 0) < deployment['metadata'].get('generation', 1)
                    or status.get('availableReplicas', 0) < desired
                    or status.get('updatedReplicas', 0) < desired):
                return False
        return True
