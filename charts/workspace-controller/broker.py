#!/usr/bin/env python3
"""broker.py — the privileged provisioning broker (#421).

WHY THIS PROCESS EXISTS
=======================
Provisioning a workspace needs cluster-wide create verbs (namespaces, RBAC,
Secrets, Deployments). Until #421 the internet-facing controller held
`create jobs` in its own namespace and the Job it created *selected* the
broadly-privileged `workspace-provisioner` ServiceAccount. Kubernetes hands a
pod a token for whatever SA it names, so that single namespaced verb bridged a
controller compromise straight to the provisioner's ClusterRole. #416's
ValidatingAdmissionPolicy and #420's immutable chart refs constrained the shape
of that Job; neither removed the bridge.

This broker removes it. The privileged half now lives in its OWN namespace
(`provision.broker.namespace`), and the controller has no workload-creation
rights there at all. The only thing the controller may do is `create` a
`ProvisionRequest` custom resource whose entire spec is one field:

    spec:
      slug: octo

Everything else that shapes the privileged Job — image, entrypoint, env,
serviceAccountName, volumes, resources, namespace, chart ref — is resolved
HERE, from this process's own configuration, and can never be influenced by
the request. The Kubernetes API server is the authentication boundary between
the two halves, which is why this is a CRD and not an HTTP API: there is no
shared secret, no internal endpoint to harden, and no mTLS to get wrong.

WHAT THE REQUEST CANNOT DO
==========================
The CRD's structural schema lists exactly one property under `spec`, so the API
server PRUNES anything else before the object is ever persisted — an attacker
who can create a ProvisionRequest cannot smuggle `spec.image` past it, because
the field does not survive the write. (Pruning, not rejection: Kubernetes
structural schemas drop unknown fields rather than erroring. The security
outcome is the same — the broker never sees them — but do not describe it as
"the API server rejects extra fields".) `slug` is additionally re-validated
here against the same pattern the CRD pins, so a schema regression cannot turn
a slug into a shell argument.

RELATIONSHIP TO #422
====================
This design only works because the Job needs no caller-supplied `command`:
provisioner/provision.sh is the provisioner image's ENTRYPOINT (#422 item 1).
Were the program still injected as `command`, "immutable template" would be a
fiction — whoever built the manifest would still be choosing the code that runs
at provisioner privilege. Do not reintroduce a command here without also
relaxing provisioner-vap.yaml, which would undo the guarantee.
"""

import calendar
import json
import os
import re
import subprocess
import sys
import time

# --- configuration (all broker-side; none of it is request-controlled) -------

# The broker's own namespace: where ProvisionRequests are read from and where
# the privileged Jobs are created. The controller holds no workload verbs here.
BROKER_NAMESPACE = os.environ.get('BROKER_NAMESPACE', 'kube-coder-provision').strip()
# The control-plane namespace the workspace deploy copies the shared `regcred`
# image-pull Secret FROM (provision.sh's NAMESPACE contract). Distinct from the
# namespace the Job now runs in — that is BROKER_NAMESPACE.
CONTROL_PLANE_NAMESPACE = os.environ.get('CONTROL_PLANE_NAMESPACE', 'coder').strip()
WORKSPACE_PREFIX = os.environ.get('WORKSPACE_PREFIX', 'ws-')

PROVISIONER_IMAGE = os.environ.get('PROVISIONER_IMAGE', '').strip()
PROVISIONER_SA = os.environ.get('PROVISIONER_SERVICE_ACCOUNT', 'workspace-provisioner').strip()
PROVISIONER_PULL_SECRET = os.environ.get('PROVISIONER_PULL_SECRET', '').strip()

CHART_REPO = os.environ.get('CHART_REPO', '').strip()
CHART_REF = os.environ.get('CHART_REF', '').strip()
ALLOW_MUTABLE_CHART_REF = os.environ.get('ALLOW_MUTABLE_CHART_REF', '').strip().lower() in (
    '1', 'true', 'yes', 'on')
GITOPS_REPO = os.environ.get('GITOPS_REPO', '').strip()
GITOPS_BRANCH = os.environ.get('GITOPS_BRANCH', 'main').strip()
GITOPS_TOKEN = os.environ.get('GITOPS_TOKEN', '').strip()

KUBECTL_TIMEOUT = int(os.environ.get('KUBECTL_TIMEOUT', '15'))
POLL_SECONDS = float(os.environ.get('POLL_SECONDS', '5'))
# How long a finished ProvisionRequest is kept so the console can still read its
# outcome. The Job carries its own ttlSecondsAfterFinished; this is the record.
RETAIN_SECONDS = int(float(os.environ.get('RETAIN_HOURS', '24')) * 3600)

CRD_GROUP = 'kube-coder.dev'
CRD_VERSION = 'v1alpha1'
CRD_PLURAL = 'provisionrequests'
CRD_RESOURCE = f'{CRD_PLURAL}.{CRD_GROUP}'

# The slug pattern the CRD's schema pins. Re-checked here so a schema regression
# cannot widen what reaches a shell.
SLUG_RE = re.compile(r'^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$')

PHASE_PENDING = 'Pending'
PHASE_RUNNING = 'Running'
PHASE_SUCCEEDED = 'Succeeded'
PHASE_FAILED = 'Failed'
TERMINAL_PHASES = (PHASE_SUCCEEDED, PHASE_FAILED)

# Supply-chain: a chart ref the privileged Job git-clones and runs `make deploy`
# from must be immutable (finding 7). Same classification the controller used to
# do; it lives here now because the broker, not the caller, chooses the ref.
_CHART_REF_SHA_RE = re.compile(r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')
_CHART_REF_TAG_RE = re.compile(r'^v\d+\.\d+\.\d+$')


class ProvisionError(RuntimeError):
    pass


class KubectlError(RuntimeError):
    def __init__(self, message, stderr=''):
        super().__init__(message)
        self.stderr = stderr


def log(msg):
    sys.stderr.write(f'[broker] {msg}\n')
    sys.stderr.flush()


def classify_chart_ref(ref):
    """'commit-sha' (full 40/64-hex SHA), 'release-tag' (vX.Y.Z), or 'mutable'."""
    r = (ref or '').strip()
    if _CHART_REF_SHA_RE.match(r):
        return 'commit-sha'
    if _CHART_REF_TAG_RE.match(r):
        return 'release-tag'
    return 'mutable'


def validate_chart_ref(ref):
    """Fail closed on a mutable ref unless explicitly opted in (finding 7)."""
    kind = classify_chart_ref(ref)
    if kind == 'mutable' and not ALLOW_MUTABLE_CHART_REF:
        raise ProvisionError(
            f'refusing to provision from mutable chart ref {(ref or "").strip()!r}: the '
            f'provisioner Job git-clones this ref and runs its `make deploy` under the '
            f'cluster-privileged provisioner ServiceAccount, so it must be pinned to an '
            f'immutable reference — a full 40-hex commit SHA or a vX.Y.Z release tag. '
            f'Set provision.chart.ref to a pinned ref, or set '
            f'provision.chart.allowMutableRef=true (env ALLOW_MUTABLE_CHART_REF) to override.')
    return kind


def ns_for_user(slug):
    return f'{WORKSPACE_PREFIX}{slug}'


# --- kubectl plumbing --------------------------------------------------------

def _kubectl_json(args, _attempts=2):
    cmd = ['kubectl', *args, '-n', BROKER_NAMESPACE, '-o', 'json']
    last = None
    for attempt in range(_attempts):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=KUBECTL_TIMEOUT)
        except FileNotFoundError:
            raise KubectlError('kubectl not found on PATH')
        except subprocess.TimeoutExpired:
            last = KubectlError(f'kubectl timed out after {KUBECTL_TIMEOUT}s')
        else:
            if proc.returncode == 0:
                try:
                    return json.loads(proc.stdout)
                except json.JSONDecodeError as exc:
                    last = KubectlError(f'kubectl returned non-JSON: {exc}')
            else:
                last = KubectlError(f'kubectl {args[0]} failed', proc.stderr.strip())
        if attempt + 1 < _attempts:
            time.sleep(0.4)
    raise last


def _kubectl_run(args, stdin=None):
    cmd = ['kubectl', *args, '-n', BROKER_NAMESPACE]
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                              timeout=KUBECTL_TIMEOUT)
    except FileNotFoundError:
        raise KubectlError('kubectl not found on PATH')
    except subprocess.TimeoutExpired:
        raise KubectlError(f'kubectl timed out after {KUBECTL_TIMEOUT}s')
    if proc.returncode != 0:
        raise KubectlError(f'kubectl {args[0]} failed', proc.stderr.strip())
    return proc.stdout.strip()


# --- the immutable Job template ---------------------------------------------

def build_job_manifest(slug, request_name=None, request_uid=None):
    """Stamp the privileged provisioning Job for `slug`.

    Moved here from controller.py (#421). Nothing in this manifest comes from
    the request except `slug`; every other value is broker-side configuration.
    That is the whole point of the rearchitecture, so keep it that way — a new
    field read off the ProvisionRequest is a new privilege-escalation surface.

    The Job's *name* is the request's name, which makes creation idempotent: if
    the broker stamps the Job and then crashes before recording it in status,
    the next reconcile hits AlreadyExists rather than starting a second
    privileged helm-upgrade for the same workspace.
    """
    ref_kind = validate_chart_ref(CHART_REF)
    log(f'provisioning {slug}: chart repo={CHART_REPO} ref={CHART_REF} '
        f'({ref_kind}) allowMutableRef={ALLOW_MUTABLE_CHART_REF}')
    name = (request_name or f'provision-{slug}-{int(time.time())}')[:63]
    # The provisioner image is mandatory and there is no fallback to any other
    # image: the program is that image's baked ENTRYPOINT (#422), so a Job on a
    # different image would run THAT image's entrypoint and could exit 0 having
    # provisioned nothing. Fail loudly, at manifest-build time.
    if not PROVISIONER_IMAGE:
        raise ProvisionError(
            'refusing to provision: provision.image is not set. The Job runs the '
            'provisioning script baked into the dedicated provisioner image '
            '(provisioner/Dockerfile) as its entrypoint and supplies no command, so '
            'there is no usable fallback. Set provision.image to the signed '
            'provisioner image pinned by digest '
            '(ghcr.io/imran31415/kube-coder/provisioner@sha256:...).')
    env = [
        {'name': 'SLUG', 'value': slug},
        # NAMESPACE = the control-plane namespace the workspace deploy copies
        # regcred FROM; WS_NAMESPACE = the workspace's own per-user namespace
        # (#103). Since #421 the Job itself runs in the BROKER namespace, which
        # is neither of these — keep the three distinct.
        {'name': 'NAMESPACE', 'value': CONTROL_PLANE_NAMESPACE},
        {'name': 'WS_NAMESPACE', 'value': ns_for_user(slug)},
        {'name': 'CHART_REPO', 'value': CHART_REPO},
        {'name': 'CHART_REF', 'value': CHART_REF},
        {'name': 'GITOPS_REPO', 'value': GITOPS_REPO},
        {'name': 'GITOPS_BRANCH', 'value': GITOPS_BRANCH},
        {'name': 'GITOPS_TOKEN', 'value': GITOPS_TOKEN},
    ]
    container = {
        'name': 'provision',
        'image': PROVISIONER_IMAGE,
        # Deliberately NO 'command'/'args': the image's baked ENTRYPOINT is the
        # program (#422 item 1). Admission enforces the absence — do not add one
        # back without also relaxing provisioner-vap.yaml, which would undo the
        # immutable-template guarantee this whole design rests on.
        'env': env,
        'resources': {'requests': {'cpu': '100m', 'memory': '256Mi'},
                      'limits': {'cpu': '1', 'memory': '1Gi'}},
    }
    pod_spec = {
        'serviceAccountName': PROVISIONER_SA,
        'restartPolicy': 'Never',
        'containers': [container],
    }
    if PROVISIONER_PULL_SECRET:
        pod_spec['imagePullSecrets'] = [{'name': PROVISIONER_PULL_SECRET}]
    meta = {
        'name': name,
        'namespace': BROKER_NAMESPACE,
        'labels': {'app': 'workspace-provisioner', 'provisionUser': slug},
    }
    if request_uid:
        # Garbage-collect the Job with its request, so deleting a record never
        # strands a privileged pod behind it.
        meta['ownerReferences'] = [{
            'apiVersion': f'{CRD_GROUP}/{CRD_VERSION}',
            'kind': 'ProvisionRequest',
            'name': request_name,
            'uid': request_uid,
            'controller': True,
            'blockOwnerDeletion': False,
        }]
    return {
        'apiVersion': 'batch/v1',
        'kind': 'Job',
        'metadata': meta,
        'spec': {
            'backoffLimit': 1,
            'ttlSecondsAfterFinished': 3600,    # auto-clean an hour after finish
            'activeDeadlineSeconds': 900,
            'template': {
                'metadata': {'labels': {'app': 'workspace-provisioner', 'provisionUser': slug}},
                'spec': pod_spec,
            },
        },
    }


# --- reconcile ---------------------------------------------------------------

def list_requests():
    return _kubectl_json(['get', CRD_RESOURCE]).get('items', [])


def get_job(name):
    """The Job with this name, or None if it does not exist.

    Only a genuine NotFound counts as "absent". Swallowing every kubectl error
    here would turn a transient API outage — or a missing RBAC verb — into
    "no Job exists", and the next line of reconcile_one stamps a new privileged
    Job. Anything else propagates and is logged by the reconcile loop.
    """
    try:
        return _kubectl_json(['get', 'job', name], _attempts=1)
    except KubectlError as exc:
        if 'not found' in (getattr(exc, 'stderr', '') or '').lower():
            return None
        raise


def create_job(manifest):
    """Apply the Job. AlreadyExists is success — see build_job_manifest."""
    try:
        return _kubectl_run(['create', '-f', '-'], stdin=json.dumps(manifest))
    except KubectlError as exc:
        if 'already exists' in (exc.stderr or '').lower():
            return 'unchanged'
        raise


def patch_status(name, status):
    """Merge-patch the request's /status subresource.

    The status subresource matters for the trust boundary, not just tidiness:
    the controller is granted create/get/list/watch on `provisionrequests` and
    NOTHING on `provisionrequests/status`, so it can ask for a workspace and
    read the outcome but cannot forge one.
    """
    _kubectl_run(['patch', CRD_RESOURCE, name, '--subresource=status',
                  '--type=merge', '-p', json.dumps({'status': status})])


def job_phase(job):
    """Map a Job's status onto a ProvisionRequest phase + message."""
    st = (job or {}).get('status', {})
    if st.get('succeeded'):
        return PHASE_SUCCEEDED, ''
    if st.get('failed'):
        return PHASE_FAILED, 'provisioner Job failed — see Job logs'
    if st.get('active'):
        return PHASE_RUNNING, ''
    return PHASE_PENDING, ''


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def reconcile_one(req):
    """Drive one ProvisionRequest one step. Never raises — a bad request must
    not stall every other tenant's provisioning."""
    meta = req.get('metadata', {})
    name = meta.get('name', '')
    status = req.get('status') or {}
    phase = status.get('phase', '')
    slug = (req.get('spec') or {}).get('slug', '')

    if phase in TERMINAL_PHASES:
        return
    if not SLUG_RE.match(slug or ''):
        # Defense in depth behind the CRD's own pattern.
        patch_status(name, {'phase': PHASE_FAILED, 'finishedAt': _now(),
                            'message': f'invalid slug {slug!r}: must match {SLUG_RE.pattern}'})
        return

    recorded = status.get('jobName')
    job_name = recorded or name
    job = get_job(job_name)
    if job is None:
        if recorded:
            # We already stamped a Job for this request and it is gone —
            # ttlSecondsAfterFinished reaped it while the broker was down, or
            # someone deleted it. Its outcome is unknowable, so record that
            # rather than stamping a REPLACEMENT: re-running a privileged helm
            # upgrade because a record expired is a privileged action nobody
            # asked for. An admin re-runs the provision from the console.
            patch_status(name, {
                'phase': PHASE_FAILED, 'finishedAt': _now(),
                'message': (f'provisioner Job {recorded} no longer exists and its result was '
                            f'never recorded (Job TTL is 1h; was the broker down?). '
                            f'Re-run the provision to try again.')})
            log(f'{name}: Job {recorded} vanished before its result was recorded')
            return
        try:
            manifest = build_job_manifest(slug, request_name=name, request_uid=meta.get('uid'))
        except ProvisionError as exc:
            # Misconfiguration (missing image, mutable chart ref). Surface it on
            # the request so the console shows the real reason instead of a
            # request that silently never starts.
            patch_status(name, {'phase': PHASE_FAILED, 'finishedAt': _now(),
                                'message': str(exc)})
            return
        try:
            create_job(manifest)
        except KubectlError as exc:
            # A create that the API server refuses is almost always
            # deterministic — admission (the provisioner VAP rejecting a shape
            # or an unsigned/tag-only image) or a missing RBAC verb. Retrying it
            # every poll forever would be an invisible loop, so record the
            # reason on the request where an admin can read it. A genuinely
            # transient failure costs one re-run from the console.
            patch_status(name, {
                'phase': PHASE_FAILED, 'finishedAt': _now(),
                'message': f'{exc}: {getattr(exc, "stderr", "")}'[:1024]})
            log(f'{name}: Job create refused: {exc} {getattr(exc, "stderr", "")}')
            return
        log(f'{name}: stamped Job {job_name} for slug={slug}')
        # Pending, not Running: the Job has no status yet. This matches what the
        # console saw pre-#421, when provision_status read a freshly created Job.
        patch_status(name, {'phase': PHASE_PENDING, 'jobName': job_name,
                            'startedAt': status.get('startedAt') or _now(), 'message': ''})
        return

    new_phase, message = job_phase(job)
    if new_phase != phase or status.get('jobName') != job_name:
        patch = {'phase': new_phase, 'jobName': job_name, 'message': message}
        if new_phase in TERMINAL_PHASES:
            patch['finishedAt'] = _now()
        patch_status(name, patch)
        log(f'{name}: {phase or "(new)"} -> {new_phase}')


def _age_seconds(stamp):
    """Seconds since an RFC3339 UTC stamp. calendar.timegm, not time.mktime:
    mktime reads the struct as LOCAL time, which would skew retention by the
    pod's timezone offset. Unparseable => 0, so a malformed stamp is never
    mistaken for something ancient and deleted."""
    try:
        return time.time() - calendar.timegm(time.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ'))
    except (ValueError, TypeError):
        return 0.0


def collect_garbage(requests):
    """Delete finished requests older than RETAIN_SECONDS. Their Jobs go with
    them via ownerReferences, so nothing privileged is left behind."""
    for req in requests:
        status = req.get('status') or {}
        if status.get('phase') not in TERMINAL_PHASES:
            continue
        finished = status.get('finishedAt')
        if finished and _age_seconds(finished) > RETAIN_SECONDS:
            name = req.get('metadata', {}).get('name', '')
            try:
                _kubectl_run(['delete', CRD_RESOURCE, name, '--ignore-not-found'])
                log(f'{name}: retired (finished {finished})')
            except KubectlError as exc:
                log(f'{name}: retire failed: {exc}')


def reconcile_once():
    requests = list_requests()
    for req in requests:
        name = req.get('metadata', {}).get('name', '?')
        try:
            reconcile_one(req)
        except KubectlError as exc:
            log(f'{name}: reconcile failed: {exc} {getattr(exc, "stderr", "")}')
        except Exception as exc:                      # noqa: BLE001 - never die on one request
            log(f'{name}: reconcile error: {exc!r}')
    collect_garbage(requests)
    return len(requests)


def main():
    log(f'starting: namespace={BROKER_NAMESPACE} controlPlane={CONTROL_PLANE_NAMESPACE} '
        f'sa={PROVISIONER_SA} image={PROVISIONER_IMAGE or "(unset!)"} '
        f'chartRef={CHART_REF or "(unset!)"} poll={POLL_SECONDS}s')
    if not PROVISIONER_IMAGE:
        log('WARNING provision.image is unset — every request will fail closed')
    while True:
        try:
            reconcile_once()
        except KubectlError as exc:
            log(f'list failed: {exc} {getattr(exc, "stderr", "")}')
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
