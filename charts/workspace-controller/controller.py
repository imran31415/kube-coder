#!/usr/bin/env python3
"""workspace-controller — a small admin console backend.

Lists every workspace Deployment in the namespace (anything named
`<prefix><user>`, default prefix `ws-`) and lets an operator start/stop it by
scaling the Deployment to 1 or 0. Start/stop is a pure Kubernetes API
operation — there is no Helm at runtime — so this shells out to `kubectl`
(already on the image) using the pod's in-cluster ServiceAccount token.

Auth mirrors server.py's model: behind oauth2-proxy, which injects
X-Auth-Request-User after a GitHub login. We honor that header only when
TRUSTED_PROXY=true (so a misconfigured ingress can't be spoofed via
client-supplied headers), and optionally re-check it against an ADMIN_USERS
allowlist as defense-in-depth. The SPA prepends /oauth to /api calls (see
web/src/api/client.ts) so the auth cookie attaches; we strip that prefix here.

Stdlib only — no third-party deps, so it runs on the unmodified coder image.
"""
import base64
import concurrent.futures
import datetime
import hashlib
import http.server
import hmac
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Config (from env / pod spec) -------------------------------------------

NAMESPACE_FILE = '/var/run/secrets/kubernetes.io/serviceaccount/namespace'


def detect_namespace():
    try:
        with open(NAMESPACE_FILE) as f:
            return f.read().strip()
    except OSError:
        return os.environ.get('NAMESPACE', 'coder')


NAMESPACE = detect_namespace()
PORT = int(os.environ.get('CONTROLLER_PORT', '8080'))
# A workspace is any Deployment whose name starts with this prefix; the
# username is the remainder. Matches the ws-<user> convention.
WORKSPACE_PREFIX = os.environ.get('WORKSPACE_PREFIX', 'ws-')
# Only honor upstream identity headers when an auth proxy is known to strip
# client-supplied ones. Same flag/semantics as server.py.
TRUSTED_PROXY = os.environ.get('TRUSTED_PROXY', 'true').lower() == 'true'
# Optional allowlist (comma-separated GitHub usernames). Empty => trust the
# oauth2-proxy --github-user gate alone.
ADMIN_USERS = {
    u.strip().lower() for u in os.environ.get('ADMIN_USERS', '').split(',') if u.strip()
}
# Local-dev only: a bearer token that bypasses the proxy-header check, so
# `yarn dev` can hit the API without oauth2. NEVER set by the Helm chart.
DEV_TOKEN = os.environ.get('CONTROLLER_DEV_TOKEN', '')
# Persistent admin API token (opt-in, from a Secret via the Helm chart). Lets
# non-browser clients — the Expo mobile app — reach the admin API with
# `Authorization: Bearer <token>`, since oauth2-proxy can't do a mobile OAuth
# handshake. Unlike DEV_TOKEN this IS production-safe (a long random secret,
# revealed only to an already-authenticated admin). Empty => disabled.
ADMIN_TOKEN = os.environ.get('CONTROLLER_ADMIN_TOKEN', '').strip()
DIST_DIR = os.environ.get('CONTROLLER_DIST_DIR', '/controller-web')
KUBECTL_TIMEOUT = int(os.environ.get('KUBECTL_TIMEOUT', '15'))
# Max concurrent per-namespace kubectl reads in _collect(). Bounds the fan-out
# so a large fleet can't spawn hundreds of kubectl processes at once, while
# still keeping the workspace listing sub-second as namespaces grow.
COLLECT_CONCURRENCY = int(os.environ.get('COLLECT_CONCURRENCY', '16'))
MAX_REQUEST_BODY_BYTES = int(os.environ.get('MAX_REQUEST_BODY_BYTES', str(64 * 1024)))

# Metrics come from the in-cluster Prometheus (no metrics-server on this
# cluster). Empty PROMETHEUS_URL disables the metrics endpoint cleanly.
PROMETHEUS_URL = os.environ.get(
    'PROMETHEUS_URL', 'http://prometheus-kube-prometheus-prometheus.default.svc:9090'
).rstrip('/')
PROM_TIMEOUT = int(os.environ.get('PROM_TIMEOUT', '8'))
# Rough cost model — approximate, operator-tunable. Compute is billed on
# observed usage; storage on the PVC's provisioned size. Defaults derived from
# DigitalOcean Basic droplet pricing, which is linear at $12/mo per (1 vCPU +
# 2 GB): splitting that 50/50 gives ~$6/core-mo and ~$3/GB-mo, i.e. $0.0082/
# core-hr and $0.0041/GB-hr. Block storage is $0.10/GB-mo. Override via env.
COST_CPU_CORE_HOUR = float(os.environ.get('COST_CPU_CORE_HOUR', '0.0082'))
COST_MEM_GB_HOUR = float(os.environ.get('COST_MEM_GB_HOUR', '0.0041'))
COST_STORAGE_GB_MONTH = float(os.environ.get('COST_STORAGE_GB_MONTH', '0.10'))
HOURS_PER_MONTH = 730.0

# Insights: how far back to analyse usage for the dashboard tips, and the CPU
# level under which a workspace is considered idle.
INSIGHTS_WINDOW = int(os.environ.get('INSIGHTS_WINDOW_SECONDS', '21600'))  # 6h
INSIGHTS_IDLE_CPU_CORES = float(os.environ.get('INSIGHTS_IDLE_CPU_CORES', '0.05'))

# Deployment name must look like <prefix><user>; <user> is lowercase
# DNS-label-ish. This is also the canonical "is this a workspace" test. Under
# per-workspace namespaces the namespace carries the same ws-<user> name, so
# the same regex classifies both a workspace Deployment and its namespace.
_NAME_RE = re.compile(r'^' + re.escape(WORKSPACE_PREFIX) + r'([a-z0-9][a-z0-9-]{0,40})$')
_USER_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,40}$')


def ns_for_user(user):
    """The per-workspace namespace for a user — same string as the workspace's
    Deployment/ServiceAccount (ws-<user>, issue #103)."""
    return f'{WORKSPACE_PREFIX}{user}'


def _re2_literal(s):
    """Escape a literal for embedding in a PromQL (RE2) regex. Unlike
    re.escape, the hyphen is left UNescaped: Prometheus/RE2 rejects `\\-`
    outside a character class with an HTTP 400, and `-` is already a literal
    there. Escapes only the RE2 metacharacters that actually need it."""
    return re.sub(r'[.^$*+?()\[\]{}|\\]', lambda m: '\\' + m.group(0), s)


def _ws_prom_ns_selector():
    """PromQL namespace matcher covering every workspace namespace. Workspaces
    live in per-user ws-<user> namespaces (#103); the controller's own namespace
    is OR'd in so a workspace not yet migrated off the shared namespace still
    shows in fleet metrics. Paired with a pod=~"ws-.*" filter at each call site."""
    return f'namespace=~"{_re2_literal(WORKSPACE_PREFIX)}.*|{_re2_literal(NAMESPACE)}"'


class KubectlError(RuntimeError):
    def __init__(self, message, stderr=''):
        super().__init__(message)
        self.stderr = stderr


def _kubectl_json(args, _attempts=2, namespace=None):
    """Run `kubectl <args> -o json -n <ns>` and parse stdout.

    Under per-workspace namespaces (#103) callers pass the target workspace's
    namespace; `namespace=None` falls back to the controller's own namespace
    (control-plane reads/writes: provisioning Jobs, coder-resident workspaces).

    Retries once on failure: kubectl's discovery cache is cold right after a pod
    start and several read endpoints fire concurrently on page load, which can
    make the first call fail transiently. These are read-only, so a retry is safe."""
    cmd = ['kubectl', *args, '-n', namespace or NAMESPACE, '-o', 'json']
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


def _kubectl_run(args, namespace=None):
    """Run a mutating kubectl command; raise KubectlError on failure.

    `namespace=None` targets the controller's own namespace; per-workspace
    mutations (scale/patch) pass the workspace's resolved namespace (#103)."""
    cmd = ['kubectl', *args, '-n', namespace or NAMESPACE]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=KUBECTL_TIMEOUT)
    except FileNotFoundError:
        raise KubectlError('kubectl not found on PATH')
    except subprocess.TimeoutExpired:
        raise KubectlError(f'kubectl timed out after {KUBECTL_TIMEOUT}s')
    if proc.returncode != 0:
        raise KubectlError(f'kubectl {args[0]} failed', proc.stderr.strip())
    return proc.stdout.strip()


# --- Workspace listing -------------------------------------------------------

def _pod_summary(pod):
    """Compact per-pod status used to distinguish 'starting' from 'crashing'."""
    status = pod.get('status', {})
    cstatuses = status.get('containerStatuses', []) or []
    ready = bool(cstatuses) and all(c.get('ready') for c in cstatuses)
    restarts = sum(c.get('restartCount', 0) for c in cstatuses)
    reason = None
    for c in cstatuses:
        st = c.get('state', {})
        if 'waiting' in st and st['waiting'].get('reason'):
            reason = st['waiting']['reason']        # e.g. CrashLoopBackOff, ImagePullBackOff
            break
        if 'terminated' in st and st['terminated'].get('reason') not in (None, 'Completed'):
            reason = st['terminated']['reason']
            break
    return {
        'name': pod.get('metadata', {}).get('name', ''),
        'phase': status.get('phase', 'Unknown'),
        'ready': ready,
        'restarts': restarts,
        'reason': reason,
    }


def _classify(desired, ready, obs_gen, gen, pods):
    """Map replica/generation/pod facts to a UI state."""
    if desired == 0:
        return 'stopped'
    if obs_gen < gen:
        return 'transitioning'        # spec edited; controller hasn't caught up
    if ready >= desired:
        return 'running'
    # desired >= 1 but not all ready: starting vs. wedged?
    bad = {'CrashLoopBackOff', 'ImagePullBackOff', 'ErrImagePull', 'Error', 'CreateContainerError'}
    if any(p.get('reason') in bad for p in pods):
        return 'degraded'
    return 'transitioning'


def discover_workspace_namespaces():
    """Namespaces that may hold a workspace: every ws-<user> namespace plus the
    controller's own (covers a workspace not yet migrated off the shared
    namespace). Falls back to just the controller namespace if listing
    namespaces is denied/unavailable, so the console still works against the
    pre-#103 shared-namespace layout."""
    found = {NAMESPACE}
    try:
        for ns in _kubectl_json(['get', 'namespaces']).get('items', []):
            name = ns.get('metadata', {}).get('name', '')
            if _NAME_RE.match(name):
                found.add(name)
    except KubectlError:
        pass
    return sorted(found)


def _collect(resource):
    """Concatenate `kubectl get <resource>` items across all workspace
    namespaces. Every item keeps its own metadata.namespace so callers can tell
    workspaces apart. A per-namespace read failure is skipped, not fatal — one
    unreadable tenant namespace shouldn't blank the whole console.

    The per-namespace reads run concurrently: the console lists the whole fleet
    on every page load, and a sequential kubectl-per-namespace walk grew
    linearly with workspace count (~1+3N subprocess spawns per /api/workspaces),
    which is what pushed slow listings past the ingress timeout into 502s. These
    calls are I/O-bound (each blocks on the apiserver), so threads give real
    concurrency despite the GIL, and wall-time collapses to roughly the slowest
    single namespace read regardless of fleet size."""
    namespaces = discover_workspace_namespaces()

    def _one(ns):
        try:
            return _kubectl_json(['get', resource], namespace=ns).get('items', [])
        except KubectlError:
            return []

    if len(namespaces) <= 1:
        return _one(namespaces[0]) if namespaces else []

    items = []
    workers = min(COLLECT_CONCURRENCY, len(namespaces))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for chunk in pool.map(_one, namespaces):
            items.extend(chunk)
    return items


def list_workspaces():
    deps = _collect('deployments')
    all_pods = _collect('pods')
    # Hosts are best-effort; if the RBAC for ingresses is absent or the call
    # fails we still return the list, just without clickable URLs.
    hosts = {}
    try:
        for ing in _collect('ingress'):
            app = ing.get('metadata', {}).get('labels', {}).get('app')
            if not app or app in hosts:
                continue
            for rule in ing.get('spec', {}).get('rules', []):
                if rule.get('host'):
                    hosts[app] = rule['host']
                    break
    except KubectlError:
        pass

    pods_by_app = {}
    for pod in all_pods:
        app = pod.get('metadata', {}).get('labels', {}).get('app')
        if app:
            pods_by_app.setdefault(app, []).append(pod)

    out = []
    for dep in deps:
        name = dep.get('metadata', {}).get('name', '')
        m = _NAME_RE.match(name)
        if not m:
            continue
        user = m.group(1)
        spec = dep.get('spec', {})
        status = dep.get('status', {})
        desired = spec.get('replicas', 1)
        ready = status.get('readyReplicas', 0) or 0
        gen = dep.get('metadata', {}).get('generation', 0) or 0
        obs_gen = status.get('observedGeneration', 0) or 0
        pods = [_pod_summary(p) for p in pods_by_app.get(name, [])]
        state = _classify(desired, ready, obs_gen, gen, pods)
        host = hosts.get(name)
        detail = next((p['reason'] for p in pods if p.get('reason')), None)
        if detail is None:
            detail = 'stopped' if desired == 0 else f'{ready}/{desired} ready'
        image = _workspace_image(dep)
        image_tag, version = version_from_image(image)
        ws_ns = dep.get('metadata', {}).get('namespace', NAMESPACE)
        out.append({
            'user': user,
            'deployment': name,
            'namespace': ws_ns,
            # Isolated == migrated to its own per-user namespace (#103). A
            # workspace still in the control-plane namespace ($NAMESPACE) is
            # either not-yet-migrated or a leftover scaled-to-0 rollback copy —
            # the SPA badges these differently so the two don't look like
            # accidental duplicates during a migration.
            'isolated': ws_ns != NAMESPACE,
            'state': state,
            'desiredReplicas': desired,
            'readyReplicas': ready,
            'url': f'https://{host}/' if host else None,
            'pods': pods,
            'detail': detail,
            'image': image,
            'imageTag': image_tag,
            'version': version,
        })
    out.sort(key=lambda w: w['user'])
    return {'namespace': NAMESPACE, 'workspaces': out}


def _find_workspace(user):
    """The workspace dict (incl. its namespace) for a user from the live
    listing, or LookupError. Ensures we only ever mutate something the console
    would actually show, and in the namespace it really lives in (#103)."""
    if not _USER_RE.match(user):
        raise ValueError('invalid workspace name')
    name = f'{WORKSPACE_PREFIX}{user}'
    for w in list_workspaces()['workspaces']:
        if w['deployment'] == name:
            return w
    raise LookupError(name)


def scale_workspace(user, replicas):
    # Confirm the deployment exists (and is actually a workspace) before
    # touching it — never scale something the listing wouldn't show.
    ws = _find_workspace(user)
    name = ws['deployment']
    _kubectl_run(['scale', f'deployment/{name}', f'--replicas={replicas}'],
                 namespace=ws['namespace'])
    return name


# --- Resource limits ---------------------------------------------------------
#
# Bump a live workspace's CPU/memory limits straight on the Deployment, the same
# immediate-kubectl style as start/stop (no Helm at runtime). The patch targets
# only the `ide` container by name via a strategic merge, so the workspace's
# other containers and its `requests` are left untouched. Like start/stop, this
# mutates live state that a later `helm upgrade` would reset — durable changes
# still belong in the workspace's values.yaml.

# The user-facing workspace container; the metrics panel limits come from here.
WORKSPACE_CONTAINER = os.environ.get('WORKSPACE_CONTAINER', 'ide')
# Guardrails so a fat-fingered limit can't request an unschedulable pod. Tunable.
# MAX_MEM_LIMIT is parsed lazily in _validate_mem (parse_bytes is defined below).
MAX_CPU_LIMIT_CORES = float(os.environ.get('MAX_CPU_LIMIT_CORES', '16'))
MAX_MEM_LIMIT = os.environ.get('MAX_MEM_LIMIT', '64Gi')
_CPU_QTY_RE = re.compile(r'^\d+(\.\d+)?m?$')
_MEM_QTY_RE = re.compile(r'^\d+(\.\d+)?(Ki|Mi|Gi|Ti|Pi|Ei|K|M|G|T|P|E)?$')


def _validate_cpu(q):
    q = str(q).strip()
    if not _CPU_QTY_RE.match(q):
        raise ValueError(f'invalid CPU quantity: {q!r} (e.g. "500m" or "2")')
    cores = parse_cpu(q)
    if cores is None or cores <= 0:
        raise ValueError('CPU limit must be > 0')
    if cores > MAX_CPU_LIMIT_CORES:
        raise ValueError(f'CPU limit {q} exceeds max {MAX_CPU_LIMIT_CORES} cores')
    return q


def _validate_mem(q):
    q = str(q).strip()
    if not _MEM_QTY_RE.match(q):
        raise ValueError(f'invalid memory quantity: {q!r} (e.g. "4Gi" or "512Mi")')
    b = parse_bytes(q)
    if b is None or b <= 0:
        raise ValueError('memory limit must be > 0')
    cap = parse_bytes(MAX_MEM_LIMIT)
    if cap and b > cap:
        raise ValueError(f'memory limit {q} exceeds max {MAX_MEM_LIMIT}')
    return q


def set_workspace_resources(user, cpu_limit, mem_limit, persist=True):
    """Patch the ide container's CPU+memory limits (immediate) and, when GitOps is
    configured and persist=True, also commit them to the user's values.yaml so the
    change survives the next reconcile — same write-back as set_workspace_image.
    At least one limit must be given. Returns a result dict."""
    if not _USER_RE.match(user):
        raise ValueError('invalid workspace name')
    limits = {}
    if cpu_limit not in (None, ''):
        limits['cpu'] = _validate_cpu(cpu_limit)
    if mem_limit not in (None, ''):
        limits['memory'] = _validate_mem(mem_limit)
    if not limits:
        raise ValueError('provide a cpu and/or memory limit')
    ws = _find_workspace(user)
    name = ws['deployment']
    # Strategic merge: containers are merged by `name`, and the limits map merges
    # key-wise, so only the keys we send change and `requests` stays as-is.
    patch = {'spec': {'template': {'spec': {'containers': [
        {'name': WORKSPACE_CONTAINER, 'resources': {'limits': limits}}]}}}}
    _kubectl_run(['patch', f'deployment/{name}', '--type=strategic', '-p', json.dumps(patch)],
                 namespace=ws['namespace'])
    persisted = False
    persist_error = None
    if persist and GITOPS_REPO and GITOPS_TOKEN:
        try:
            persisted = gitops_update_resources(user, limits)
        except (ProvisionError, GithubError) as exc:
            persist_error = str(exc)
            sys.stderr.write(f'[controller] gitops resource update {user} failed: {exc}\n')
    return {'user': user, 'limits': limits, 'persisted': persisted, 'persistError': persist_error}


# --- Version / image-tag updates ---------------------------------------------
#
# Workspaces run a pinned image tag (registry.../coder:devlaptop-v<X.Y.Z>). A
# release publishes a matching `devlaptop-v<X.Y.Z>` image and a GitHub release
# tagged `v<X.Y.Z>`. "Update" = repoint a workspace at the latest tag: patch the
# live Deployment (immediate rollout; pullPolicy Always pulls the new image)
# and, when GitOps is configured, also commit the new tag to the user's
# values.yaml so the change survives the next `helm upgrade` reconcile. Both
# operations are a `deployments[patch]` / a git push — no new RBAC.

# Repo whose GitHub Releases define "latest". Its release `tag_name` is
# `v<X.Y.Z>`; the workspace image tag is that prefixed with `devlaptop-`.
RELEASE_REPO = os.environ.get('RELEASE_REPO', 'imran31415/kube-coder').strip()
# Cache the latest-release lookup: the unauthenticated GitHub API allows only
# 60 req/hr/IP and the dashboard polls, so a TTL keeps us well clear across the
# 2 controller replicas. A token (GITOPS_TOKEN) raises the ceiling further.
RELEASE_CHECK_TTL = int(os.environ.get('RELEASE_CHECK_TTL', '600'))  # 10 min
# Prefix the chart prepends to a version to form the image tag.
IMAGE_TAG_PREFIX = os.environ.get('IMAGE_TAG_PREFIX', 'devlaptop-')
# Fallback image repository if a Deployment somehow carries a tagless image.
DEFAULT_IMAGE_REPO = os.environ.get(
    'WORKSPACE_IMAGE_REPO', 'registry.digitalocean.com/resourceloop/coder').strip()
# Shared secret that lets a workspace's own backend (server.py) broker a
# self-service version update for ITS OWN workspace without an admin. The
# workspace calls the in-cluster controller Service directly (bypassing the
# oauth2 admin gate) and presents this token; we then authorize the action on
# the user it names. Empty => self-serve disabled (admin-only).
SELF_SERVE_TOKEN = os.environ.get('SELF_SERVE_TOKEN', '').strip()
# The self-serve endpoints listen on a SEPARATE port from the admin API. This
# is a security boundary, not a convenience: the admin API on PORT trusts the
# oauth2-proxy's X-Forwarded-User header, so its NetworkPolicy pins ingress to
# the oauth2-proxy alone. The self-serve port instead trusts ONLY the shared
# token (never an identity header) and is the only port workspace pods are
# allowed to reach — so a workspace can self-update but can never forge an
# admin header against PORT. See templates/networkpolicy.yaml.
SELF_SERVE_PORT = int(os.environ.get('SELF_SERVE_PORT', '8081'))

# Routes permitted on the restricted self-serve listener. Anything else 404s
# there, so the header-trusting admin routes remain unreachable from workspace
# pods even though the same Handler class implements them.
_SELF_SERVE_GET_RE = re.compile(r'^/api/self/workspaces/[a-z0-9-]{1,41}/version$')
_SELF_SERVE_POST_RE = re.compile(r'^/api/self/workspaces/[a-z0-9-]{1,41}/update$')

_SEMVER_RE = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)$')


def parse_version(s):
    """'v1.4.0' / '1.4.0' -> (1, 4, 0); None if it isn't MAJOR.MINOR.PATCH."""
    if not s:
        return None
    m = _SEMVER_RE.match(str(s).strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def version_from_image(image):
    """(raw_tag, semver_str) for a workspace image ref, or (tag|None, None).

    'registry/coder:devlaptop-v1.4.0' -> ('devlaptop-v1.4.0', 'v1.4.0').
    The version is only returned when it parses as semver, so a 'latest' or
    digest-pinned image yields (tag, None) and reads as "version unknown".
    """
    if not image or ':' not in image:
        return (None, None)
    tag = image.rsplit(':', 1)[1]
    ver = tag[len(IMAGE_TAG_PREFIX):] if tag.startswith(IMAGE_TAG_PREFIX) else tag
    return (tag, ver if parse_version(ver) else None)


def update_available(current, latest):
    """True iff both parse as semver and current is strictly older than latest."""
    c, l = parse_version(current), parse_version(latest)
    return bool(c and l and c < l)


_latest_cache = {'ts': 0.0, 'version': None}


def latest_version(force=False):
    """Latest released version string ('v1.4.0'), cached for RELEASE_CHECK_TTL.

    Best-effort: any GitHub API failure returns the last cached value (or None)
    rather than raising, so the dashboard degrades to "no update info" instead
    of erroring. The timestamp is refreshed even on failure so a down API isn't
    hammered every request.
    """
    now = time.time()
    if (not force and _latest_cache['version'] is not None
            and now - _latest_cache['ts'] < RELEASE_CHECK_TTL):
        return _latest_cache['version']
    try:
        rel = _github_api('GET', f'/repos/{RELEASE_REPO}/releases/latest',
                          token=GITOPS_TOKEN or None)
        tag = (rel or {}).get('tag_name')
        if parse_version(tag):
            _latest_cache['version'] = tag
    except GithubError as exc:
        sys.stderr.write(f'[controller] latest_version lookup failed: {exc}\n')
    _latest_cache['ts'] = now
    return _latest_cache['version']


def _workspace_image(dep):
    """The ide container's image from a Deployment (falls back to container 0)."""
    containers = (dep.get('spec', {}).get('template', {})
                  .get('spec', {}).get('containers', []) or [])
    for c in containers:
        if c.get('name') == WORKSPACE_CONTAINER:
            return c.get('image')
    return containers[0].get('image') if containers else None


def decorate_with_updates(resp):
    """Add latestVersion + per-workspace updateAvailable to a list response.

    Kept out of list_workspaces() itself so the internal existence checks in
    scale/resources/update never trigger a (cached, but still potentially
    networked) release lookup. Mutates and returns `resp`.
    """
    latest = latest_version()
    resp['latestVersion'] = latest
    for w in resp.get('workspaces', []):
        w['updateAvailable'] = update_available(w.get('version'), latest)
    return resp


def _swap_image_tag(content, new_tag):
    """Rewrite the image.tag line in a values.yaml body. Returns (new_content,
    changed). Only a `tag:` line whose value is a devlaptop-* image tag is
    touched, so an unrelated `tag:` key elsewhere in the file is never matched.
    """
    pattern = r'(?m)^(\s*tag:[ \t]*)' + re.escape(IMAGE_TAG_PREFIX) + r'\S+([ \t]*)$'
    new_content, n = re.subn(pattern, r'\g<1>' + new_tag + r'\g<2>', content, count=1)
    return (new_content, n > 0 and new_content != content)


def _swap_resource_limits(content, limits):
    """Rewrite cpu/memory values inside the `resources.limits` block of a
    values.yaml body. `limits` may carry 'cpu' and/or 'memory'. Only the
    `limits:` sub-block is touched — the `requests:` block just above it (same
    keys) is left alone. Returns (new_content, changed). Line/regex based to
    avoid a YAML dependency, matching _swap_image_tag's approach.
    """
    if not limits:
        return content, False
    # Isolate the `limits:` header and the deeper-indented body that follows it,
    # ending at the first line whose indent returns to <= the header's.
    m = re.search(r'(?m)^([ \t]*)limits:[ \t]*$', content)
    if not m:
        return content, False
    header_indent = len(m.group(1))
    nl = content.find('\n', m.end())
    if nl == -1:
        return content, False
    body_start = nl + 1
    i = body_start
    while i < len(content):
        end = content.find('\n', i)
        end = len(content) if end == -1 else end + 1
        line = content[i:end]
        if line.strip() != '':
            indent = len(line) - len(line.lstrip(' \t'))
            if indent <= header_indent:       # dedent → limits block ended
                break
        i = end
    body = content[body_start:i]
    new_body = body
    changed = False
    for key in ('cpu', 'memory'):
        val = limits.get(key)
        if val is None:
            continue
        pat = r'(?m)^([ \t]*' + key + r':[ \t]*).*$'
        new_body2, n = re.subn(pat, lambda mm, v=val: mm.group(1) + f'"{v}"', new_body, count=1)
        if n and new_body2 != new_body:
            new_body = new_body2
            changed = True
    if not changed:
        return content, False
    return content[:body_start] + new_body + content[i:], True


def _gitops_edit_values(slug, edit, commit_msg):
    """Shared clone→edit→commit→push spine for per-field GitOps updates. Clones
    the repo, applies `edit` (content -> (new_content, changed)) to the user's
    values.yaml, and commits+pushes iff it changed. Returns True if a change was
    pushed, False if the repo/file was absent or nothing changed. Raises
    ProvisionError on a git failure."""
    if not (GITOPS_REPO and GITOPS_TOKEN):
        return False
    workdir = tempfile.mkdtemp(prefix='gitops-edit-', dir='/tmp')
    try:
        repo_url = f'https://x-access-token:{GITOPS_TOKEN}@{GITOPS_REPO}'
        _git(['clone', '--depth', '1', '-b', GITOPS_BRANCH, repo_url, workdir])
        vpath = os.path.join(workdir, 'users-private', slug, 'values.yaml')
        if not os.path.isfile(vpath):
            return False
        with open(vpath) as f:
            content = f.read()
        new_content, changed = edit(content)
        if not changed:
            return False
        with open(vpath, 'w') as f:
            f.write(new_content)
        _git(['-C', workdir, 'add', '-A'])
        if not _git(['-C', workdir, 'status', '--porcelain']).strip():
            return False
        _git(['-C', workdir,
              '-c', 'user.email=controller@kube-coder',
              '-c', 'user.name=workspace-controller',
              'commit', '-m', commit_msg])
        _git(['-C', workdir, 'push', 'origin', GITOPS_BRANCH])
        return True
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def gitops_update_image_tag(slug, new_tag):
    """Repoint just the image.tag in an existing user's values.yaml in the GitOps
    repo. Returns True iff a change was committed+pushed."""
    return _gitops_edit_values(slug, lambda c: _swap_image_tag(c, new_tag),
                               f'update {slug} image to {new_tag}')


def gitops_update_resources(slug, limits):
    """Persist cpu/memory limit edits to an existing user's values.yaml in the
    GitOps repo so they survive the next reconcile. Returns True iff pushed."""
    summary = ', '.join(f'{k}={v}' for k, v in limits.items())
    return _gitops_edit_values(slug, lambda c: _swap_resource_limits(c, limits),
                               f'update {slug} resource limits ({summary})')


def set_workspace_image(user, target_version=None, persist=True):
    """Repoint a workspace at a release version.

    Patches the live Deployment's ide-container image tag (an immediate rollout)
    and, when GitOps is configured and persist=True, also commits the new tag to
    the user's values.yaml so it survives the next reconcile. target_version
    defaults to the latest release. Returns a result dict.
    """
    if not _USER_RE.match(user):
        raise ValueError('invalid workspace name')
    target = (target_version or latest_version() or '').strip()
    if not parse_version(target):
        raise ValueError('no target version available (latest release unknown)')
    name = f'{WORKSPACE_PREFIX}{user}'
    wss = {w['deployment']: w for w in list_workspaces()['workspaces']}
    ws = wss.get(name)
    if ws is None:
        raise LookupError(name)
    current_image = ws.get('image') or ''
    current_ver = ws.get('version')
    repo = current_image.rsplit(':', 1)[0] if ':' in current_image else (
        current_image or DEFAULT_IMAGE_REPO)
    new_tag = f'{IMAGE_TAG_PREFIX}{target}'
    new_image = f'{repo}:{new_tag}'
    already = (current_ver is not None and parse_version(current_ver) == parse_version(target))
    if not already:
        patch = {'spec': {'template': {'spec': {'containers': [
            {'name': WORKSPACE_CONTAINER, 'image': new_image}]}}}}
        _kubectl_run(['patch', f'deployment/{name}', '--type=strategic', '-p', json.dumps(patch)],
                     namespace=ws.get('namespace'))
    persisted = False
    persist_error = None
    if persist and GITOPS_REPO and GITOPS_TOKEN:
        try:
            persisted = gitops_update_image_tag(user, new_tag)
        except (ProvisionError, GithubError) as exc:
            persist_error = str(exc)
            sys.stderr.write(f'[controller] gitops image update {user}->{new_tag} failed: {exc}\n')
    return {
        'user': user,
        'fromVersion': current_ver,
        'toVersion': target,
        'imageTag': new_tag,
        'image': new_image,
        'rolled': not already,
        'persisted': persisted,
        'persistError': persist_error,
    }


def workspace_version_info(user):
    """Current vs latest version for one workspace (for the self-serve GET)."""
    if not _USER_RE.match(user):
        raise ValueError('invalid workspace name')
    name = f'{WORKSPACE_PREFIX}{user}'
    ws = {w['deployment']: w for w in list_workspaces()['workspaces']}.get(name)
    if ws is None:
        raise LookupError(name)
    latest = latest_version()
    return {
        'user': user,
        'version': ws.get('version'),
        'imageTag': ws.get('imageTag'),
        'latestVersion': latest,
        'updateAvailable': update_available(ws.get('version'), latest),
        'state': ws.get('state'),
    }


# --- Metrics (Prometheus) ----------------------------------------------------

class PromError(RuntimeError):
    pass


def parse_cpu(q):
    """Kubernetes CPU quantity -> cores. '2'->2.0, '500m'->0.5."""
    if not q:
        return None
    q = str(q)
    return float(q[:-1]) / 1000.0 if q.endswith('m') else float(q)


# Longest suffixes first so 'Gi' wins over 'G'.
_MEM_UNITS = [
    ('Ki', 1024), ('Mi', 1024**2), ('Gi', 1024**3), ('Ti', 1024**4), ('Pi', 1024**5),
    ('K', 1e3), ('M', 1e6), ('G', 1e9), ('T', 1e12), ('P', 1e15), ('k', 1e3),
]


def parse_bytes(q):
    """Kubernetes memory/storage quantity -> bytes. '6Gi', '512Mi', '1000000'."""
    if not q:
        return None
    q = str(q)
    for unit, mult in _MEM_UNITS:
        if q.endswith(unit):
            return float(q[:-len(unit)]) * mult
    return float(q)


def _prom_get(path, params):
    if not PROMETHEUS_URL:
        raise PromError('metrics disabled (PROMETHEUS_URL unset)')
    url = f'{PROMETHEUS_URL}{path}?{urllib.parse.urlencode(params)}'
    try:
        with urllib.request.urlopen(url, timeout=PROM_TIMEOUT) as resp:
            data = json.load(resp)
    except (OSError, ValueError) as exc:  # URLError/HTTPError/timeout subclass OSError
        raise PromError(f'prometheus unreachable: {exc}')
    if data.get('status') != 'success':
        raise PromError(data.get('error', 'prometheus query failed'))
    return data['data']


def prom_scalar(expr):
    """First value of an instant query, or None if no series."""
    res = _prom_get('/api/v1/query', {'query': expr})['result']
    return float(res[0]['value'][1]) if res else None


def prom_range(expr, seconds, step):
    """[[unix_ts, value], ...] for a range query (single series), or []."""
    end = int(time.time())
    res = _prom_get('/api/v1/query_range',
                    {'query': expr, 'start': end - seconds, 'end': end, 'step': step})['result']
    return [[int(float(t)), float(v)] for t, v in res[0]['values']] if res else []


def prom_instant_multi(expr):
    """[(labels, value), ...] for an instant query — all series."""
    res = _prom_get('/api/v1/query', {'query': expr})['result']
    return [(r['metric'], float(r['value'][1])) for r in res]


def prom_range_multi(expr, seconds, step):
    """[(labels, [values...]), ...] for a range query — all series."""
    end = int(time.time())
    res = _prom_get('/api/v1/query_range',
                    {'query': expr, 'start': end - seconds, 'end': end, 'step': step})['result']
    return [(s['metric'], [float(v) for _, v in s['values']]) for s in res]


def _cost(cpu_cores, mem_bytes, storage_bytes):
    """Rough monthly cost: compute on observed usage + storage on PVC size."""
    compute_hr = cpu_cores * COST_CPU_CORE_HOUR + (mem_bytes / 1e9) * COST_MEM_GB_HOUR
    compute_mo = compute_hr * HOURS_PER_MONTH
    storage_mo = (storage_bytes / 1e9) * COST_STORAGE_GB_MONTH
    return {
        'perHour': round(compute_hr + storage_mo / HOURS_PER_MONTH, 4),
        'computePerMonth': round(compute_mo, 2),
        'storagePerMonth': round(storage_mo, 2),
        'perMonth': round(compute_mo + storage_mo, 2),
    }


def workspace_metrics(user, range_seconds=3600, step=300):
    """Per-workspace mini-dashboard payload: current values, sparklines, cost.

    Resource limits + PVC size come from the k8s API (work even when stopped);
    live CPU/mem/disk/network + history come from Prometheus (running only).
    Prometheus failures are captured in `metricsError` rather than raised, so a
    transient outage degrades the panel instead of erroring the whole request.
    """
    name = f'{WORKSPACE_PREFIX}{user}'
    # Resolve which namespace this workspace lives in: its own ws-<user>
    # namespace (#103), falling back to the controller's namespace for a
    # workspace not yet migrated. The resolved ns scopes both the k8s reads
    # below and the Prometheus selectors further down.
    dep, ns = None, None
    candidates = [ns_for_user(user)] + ([NAMESPACE] if NAMESPACE != ns_for_user(user) else [])
    for cand in candidates:
        try:
            dep = _kubectl_json(['get', f'deployment/{name}'], namespace=cand)
            ns = cand
            break
        except KubectlError:
            continue
    if dep is None:
        raise LookupError(name)

    spec = dep.get('spec', {})
    desired = spec.get('replicas', 1)
    containers = spec.get('template', {}).get('spec', {}).get('containers', [])
    cpu_limit = sum(c for c in (parse_cpu((cn.get('resources', {}).get('limits', {}) or {}).get('cpu'))
                                for cn in containers) if c) or None
    mem_limit = sum(c for c in (parse_bytes((cn.get('resources', {}).get('limits', {}) or {}).get('memory'))
                                for cn in containers) if c) or None

    pvc_bytes = None
    try:
        pvc = _kubectl_json(['get', f'pvc/{name}-home'], namespace=ns)
        cap = ((pvc.get('status', {}).get('capacity', {}) or {}).get('storage')
               or (pvc.get('spec', {}).get('resources', {}).get('requests', {}) or {}).get('storage'))
        pvc_bytes = parse_bytes(cap)
    except KubectlError:
        pass

    pod_re = f'{name}-.*'
    # ns resolved above (the workspace's own namespace).
    out = {
        'user': user,
        'namespace': ns,
        'running': desired >= 1,
        'cpu': {'cores': None, 'limitCores': cpu_limit, 'pct': None},
        'memory': {'bytes': None, 'limitBytes': mem_limit, 'pct': None},
        'disk': {'usedBytes': None, 'capacityBytes': pvc_bytes, 'pct': None},
        'network': {'rxBps': None, 'txBps': None},
        'uptimeSeconds': None,
        'cost': _cost(0.0, 0.0, pvc_bytes or 0.0),
        'spark': {'rangeSeconds': range_seconds, 'step': step, 'cpu': [], 'memory': [], 'disk': []},
        'metricsError': None,
    }

    # `avg by (pod, container)` collapses duplicate series: this cluster runs
    # multiple kube-prometheus-stacks, so cadvisor metrics are scraped several
    # times over and a plain sum() would multiply usage. disk uses max() for
    # the same reason.
    cpu_expr = f'sum(avg by (pod, container) (rate(container_cpu_usage_seconds_total{{namespace="{ns}",pod=~"{pod_re}",container!=""}}[5m])))'
    mem_expr = f'sum(avg by (pod, container) (container_memory_working_set_bytes{{namespace="{ns}",pod=~"{pod_re}",container!=""}}))'
    disk_expr = f'max(kubelet_volume_stats_used_bytes{{namespace="{ns}",persistentvolumeclaim="{name}-home"}})'
    try:
        cpu = prom_scalar(cpu_expr)
        mem = prom_scalar(mem_expr)
        disk = prom_scalar(disk_expr)
        rx = prom_scalar(f'sum(avg by (pod, interface) (rate(container_network_receive_bytes_total{{namespace="{ns}",pod=~"{pod_re}"}}[5m])))')
        tx = prom_scalar(f'sum(avg by (pod, interface) (rate(container_network_transmit_bytes_total{{namespace="{ns}",pod=~"{pod_re}"}}[5m])))')
        start_ts = prom_scalar(f'min(kube_pod_start_time{{namespace="{ns}",pod=~"{pod_re}"}})')

        out['cpu']['cores'] = cpu
        out['memory']['bytes'] = mem
        out['disk']['usedBytes'] = disk
        out['network'] = {'rxBps': rx, 'txBps': tx}
        if cpu is not None and cpu_limit:
            out['cpu']['pct'] = round(100 * cpu / cpu_limit, 1)
        if mem is not None and mem_limit:
            out['memory']['pct'] = round(100 * mem / mem_limit, 1)
        if disk is not None and pvc_bytes:
            out['disk']['pct'] = round(100 * disk / pvc_bytes, 1)
        if start_ts:
            out['uptimeSeconds'] = max(0, int(time.time() - start_ts))

        out['spark']['cpu'] = prom_range(cpu_expr, range_seconds, step)
        out['spark']['memory'] = prom_range(mem_expr, range_seconds, step)
        out['spark']['disk'] = prom_range(disk_expr, range_seconds, step)

        out['cost'] = _cost(cpu or 0.0, mem or 0.0, pvc_bytes or 0.0)
    except PromError as exc:
        out['metricsError'] = str(exc)
    return out


# --- Insights (automatic advisories) ----------------------------------------

_SEVERITY_RANK = {'critical': 0, 'warn': 1, 'info': 2}


def _fmt_bytes(b):
    if not b:
        return '0 B'
    if b >= 1e9:
        return f'{b / 1e9:.1f} GB'
    if b >= 1e6:
        return f'{b / 1e6:.0f} MB'
    return f'{b / 1e3:.0f} KB'


def _fmt_dur(seconds):
    h = seconds / 3600.0
    if h < 1:
        return f'{seconds / 60:.0f}m'
    if h < 1.5:
        return f'{h:.1f}h'
    return f'{h:.0f}h'


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _user_for_pod(pod, names):
    """Map a Prometheus pod label (ws-<user>-<rs>-<hash>) to a username via the
    known deployment names."""
    for dep_name, user in names.items():
        if pod.startswith(dep_name + '-'):
            return user
    return None


def _advise(user, f, cpu_vals, mem_vals, disk_used, window):
    """Heuristic tips for one workspace. Conservative — each rule needs a clear
    signal to avoid noisy false positives."""
    adv = []

    def add(severity, kind, message):
        adv.append({'user': user, 'severity': severity, 'kind': kind, 'message': message})

    pvc = f['pvc_bytes']
    # Use `is not None` (not truthy) so a freshly-started workspace with
    # uptime=0 doesn't reset `covered` to the full window and trigger a
    # spurious "idle for 6h" advisory.
    covered = min(window, f['uptime']) if f['uptime'] is not None else window
    hstr = _fmt_dur(covered)

    if not f['running']:
        if pvc:
            storage_mo = (pvc / 1e9) * COST_STORAGE_GB_MONTH
            if storage_mo >= 0.5:
                add('info', 'stopped-storage',
                    f"{user}'s workspace is stopped, but its {_fmt_bytes(pvc)} disk still costs "
                    f"~${storage_mo:.2f}/mo.")
        return adv

    if not f['ready']:
        r = f.get('reason')
        add('warn', 'unhealthy',
            f"{user}'s workspace is running but not ready{f' ({r})' if r else ''} — check the pod.")
    if f['restarts'] >= 5:
        add('warn', 'restarts',
            f"{user}'s workspace has restarted {f['restarts']} times — it may be unstable.")

    cpu_limit, mem_limit = f['cpu_limit'], f['mem_limit']
    cpu_mean = _mean(cpu_vals)
    cpu_max = max(cpu_vals) if cpu_vals else 0.0
    mem_mean = _mean(mem_vals)
    mem_min = min(mem_vals) if mem_vals else 0.0
    mem_max = max(mem_vals) if mem_vals else 0.0
    mem_last = mem_vals[-1] if mem_vals else 0.0

    # Time-based tips need enough history (>= 1h covered, >= 6 samples).
    if len(cpu_vals) >= 6 and covered >= 3600:
        flat_mem = mem_mean > 0 and (mem_max - mem_min) / mem_mean < 0.15
        elevated_mem = mem_mean > max(1e9, 0.3 * (mem_limit or 0))
        idle_cpu = cpu_max < INSIGHTS_IDLE_CPU_CORES
        if idle_cpu and flat_mem and elevated_mem:
            add('warn', 'lingering-mem',
                f"{user}'s workspace has held a steady {_fmt_bytes(mem_mean)} of memory for the last "
                f"{hstr} with almost no CPU — consider checking for a lingering process.")
        elif idle_cpu:
            compute_mo = (cpu_mean * COST_CPU_CORE_HOUR + (mem_mean / 1e9) * COST_MEM_GB_HOUR) * HOURS_PER_MONTH
            tail = f" Stopping it would free compute (~${compute_mo:.2f}/mo)." if compute_mo >= 0.5 \
                else " Consider stopping it."
            add('info', 'idle',
                f"{user}'s workspace has been idle (CPU under {int(INSIGHTS_IDLE_CPU_CORES * 1000)}m) "
                f"for the last {hstr}.{tail}")
        elif cpu_limit and cpu_mean > 0.8 * cpu_limit:
            add('warn', 'high-cpu',
                f"{user}'s workspace has averaged {cpu_mean / cpu_limit * 100:.0f}% CPU over the last {hstr}.")

    if mem_limit and mem_last > 0.9 * mem_limit:
        add('critical', 'oom-risk',
            f"{user}'s workspace memory is at {mem_last / mem_limit * 100:.0f}% of its limit — "
            f"risk of an OOM kill.")
    if pvc and disk_used:
        dpct = disk_used / pvc * 100
        if dpct >= 95:
            add('critical', 'disk-full',
                f"{user}'s workspace disk is {dpct:.0f}% full — free space or grow the PVC.")
        elif dpct >= 85:
            add('warn', 'disk-high', f"{user}'s workspace disk is {dpct:.0f}% full.")
    return adv


def compute_insights(window_seconds=None):
    window = max(1800, min(window_seconds or INSIGHTS_WINDOW, 604800))
    step = max(120, window // 60)
    out = {'generatedAt': int(time.time()), 'windowSeconds': window, 'advisories': [], 'error': None}

    try:
        deps = _collect('deployments')
        pods = _collect('pods')
    except KubectlError as exc:
        out['error'] = str(exc)
        return out
    # PVC sizes are only needed for disk %-used and storage-cost tips — best
    # effort so a missing read (or RBAC gap) degrades those tips, not all of them.
    pvcs = _collect('pvc')

    pvc_cap = {}
    for p in pvcs:
        nm = p.get('metadata', {}).get('name', '')
        cap = ((p.get('status', {}).get('capacity', {}) or {}).get('storage')
               or (p.get('spec', {}).get('resources', {}).get('requests', {}) or {}).get('storage'))
        pvc_cap[nm] = parse_bytes(cap)

    pods_by_app = {}
    for p in pods:
        app = p.get('metadata', {}).get('labels', {}).get('app')
        if app:
            pods_by_app.setdefault(app, []).append(p)

    facts, names = {}, {}
    for dep in deps:
        name = dep.get('metadata', {}).get('name', '')
        m = _NAME_RE.match(name)
        if not m:
            continue
        user = m.group(1)
        names[name] = user
        spec = dep.get('spec', {})
        status = dep.get('status', {})
        containers = spec.get('template', {}).get('spec', {}).get('containers', [])
        cpu_limit = sum(c for c in (parse_cpu((cn.get('resources', {}).get('limits', {}) or {}).get('cpu'))
                                    for cn in containers) if c) or None
        mem_limit = sum(c for c in (parse_bytes((cn.get('resources', {}).get('limits', {}) or {}).get('memory'))
                                    for cn in containers) if c) or None
        uptime, restarts, reason = None, 0, None
        for pod in pods_by_app.get(name, []):
            st = pod.get('status', {})
            if st.get('startTime'):
                try:
                    t0 = datetime.datetime.fromisoformat(st['startTime'].replace('Z', '+00:00')).timestamp()
                    uptime = int(time.time() - t0)
                except ValueError:
                    pass
            for cs in st.get('containerStatuses', []) or []:
                restarts += cs.get('restartCount', 0)
                waiting = cs.get('state', {}).get('waiting')
                if waiting and waiting.get('reason'):
                    reason = waiting['reason']
        facts[user] = {
            'name': name,
            'running': spec.get('replicas', 1) >= 1,
            'ready': (status.get('readyReplicas', 0) or 0) >= 1,
            'cpu_limit': cpu_limit, 'mem_limit': mem_limit,
            'pvc_bytes': pvc_cap.get(f'{name}-home'),
            'uptime': uptime, 'restarts': restarts, 'reason': reason,
        }

    ns_sel = _ws_prom_ns_selector()
    cpu_by_user, mem_by_user, disk_by_user = {}, {}, {}
    try:
        for metric, vals in prom_range_multi(
                f'sum by (pod) (avg by (pod, container) (rate(container_cpu_usage_seconds_total{{{ns_sel},pod=~"ws-.*",container!=""}}[5m])))',
                window, step):
            u = _user_for_pod(metric.get('pod', ''), names)
            if u:
                cpu_by_user[u] = vals
        for metric, vals in prom_range_multi(
                f'sum by (pod) (avg by (pod, container) (container_memory_working_set_bytes{{{ns_sel},pod=~"ws-.*",container!=""}}))',
                window, step):
            u = _user_for_pod(metric.get('pod', ''), names)
            if u:
                mem_by_user[u] = vals
        for metric, val in prom_instant_multi(
                f'max by (persistentvolumeclaim) (kubelet_volume_stats_used_bytes{{{ns_sel},persistentvolumeclaim=~"ws-.*-home"}})'):
            pvc_name = metric.get('persistentvolumeclaim', '')
            u = names.get(pvc_name[:-5]) if pvc_name.endswith('-home') else None
            if u:
                disk_by_user[u] = val
    except PromError as exc:
        out['error'] = str(exc)

    for user, f in facts.items():
        out['advisories'].extend(
            _advise(user, f, cpu_by_user.get(user, []), mem_by_user.get(user, []),
                    disk_by_user.get(user), window))
    out['advisories'].sort(key=lambda a: (_SEVERITY_RANK.get(a['severity'], 9), a['user']))
    return out


# --- Cluster capacity rollup -------------------------------------------------
#
# "Are my workspaces about to hit the cluster's limits?" The denominator (node
# allocatable) and every usage number come from Prometheus, NOT the k8s API:
# the controller's RBAC is a namespace-scoped Role with no `nodes` read (see
# templates/serviceaccount.yaml), and that boundary is intentional on shared
# clusters. kube-state-metrics' `kube_node_status_allocatable` gives capacity
# without any extra grant, and works for every kube-prometheus-stack user.
#
# cadvisor container series carry no `node` label, so per-node usage is a join
# onto `kube_pod_info{namespace,pod,node}` keyed on (namespace, pod). The same
# `avg by (...)` dedup the per-workspace queries use is applied to the node and
# pod-info series too, so a cluster scraped by multiple Prometheis isn't double
# counted. "cluster" usage is all scheduled workload (every namespace) so the
# operator sees true headroom; "workspace" is just the ws-* pods. Their
# difference is other tenants sharing the node.


def _resource_block(allocatable, workspace, cluster):
    """One resource's rollup: raw values + percentages of allocatable.

    `other` is non-workspace usage on the same capacity (cluster minus
    workspace), clamped at 0 so scrape-skew can't render a negative bar.
    Percentages are None when allocatable is unknown or zero so the UI can show
    a dash instead of dividing by nothing."""
    def pct(v):
        if allocatable and allocatable > 0 and v is not None:
            return round(100 * v / allocatable, 1)
        return None

    other = max(0.0, cluster - workspace) if cluster is not None and workspace is not None else None
    return {
        'allocatable': allocatable,
        'workspace': workspace,
        'cluster': cluster,
        'other': other,
        'workspacePct': pct(workspace),
        'clusterPct': pct(cluster),
    }


def _node_rollup(alloc_cpu, alloc_mem, alloc_pods,
                 ws_cpu, ws_mem, tot_cpu, tot_mem, pods_ws, pods_tot):
    """Fold per-node label->value maps into a sorted node list and the cluster
    totals derived from them (so the cluster row always equals the sum of its
    nodes — no separate query that could disagree). Pure: no I/O, so the whole
    shape is unit-testable without a live Prometheus.

    Allocatable defines which nodes exist; usage maps are best-effort and
    default to 0 for a node that has no matching usage series yet (e.g. a node
    that just joined, or one running no workspaces)."""
    nodes = []
    sums = {'cpu_a': 0.0, 'cpu_w': 0.0, 'cpu_c': 0.0,
            'mem_a': 0.0, 'mem_w': 0.0, 'mem_c': 0.0,
            'pod_a': 0, 'pod_w': 0, 'pod_c': 0}
    have_cpu_a = have_mem_a = False
    for name in sorted(alloc_cpu):
        a_cpu, a_mem = alloc_cpu.get(name), alloc_mem.get(name)
        a_pod = int(alloc_pods.get(name, 0))
        w_cpu, w_mem = ws_cpu.get(name, 0.0), ws_mem.get(name, 0.0)
        t_cpu, t_mem = tot_cpu.get(name, 0.0), tot_mem.get(name, 0.0)
        p_ws, p_tot = int(pods_ws.get(name, 0)), int(pods_tot.get(name, 0))
        nodes.append({
            'name': name,
            'cpu': _resource_block(a_cpu, w_cpu, t_cpu),
            'memory': _resource_block(a_mem, w_mem, t_mem),
            'pods': {'allocatable': a_pod or None, 'workspace': p_ws, 'cluster': p_tot},
        })
        if a_cpu is not None:
            sums['cpu_a'] += a_cpu; have_cpu_a = True
        if a_mem is not None:
            sums['mem_a'] += a_mem; have_mem_a = True
        sums['cpu_w'] += w_cpu; sums['cpu_c'] += t_cpu
        sums['mem_w'] += w_mem; sums['mem_c'] += t_mem
        sums['pod_a'] += a_pod; sums['pod_w'] += p_ws; sums['pod_c'] += p_tot
    cluster = {
        'nodeCount': len(nodes),
        'cpu': _resource_block(sums['cpu_a'] if have_cpu_a else None, sums['cpu_w'], sums['cpu_c']),
        'memory': _resource_block(sums['mem_a'] if have_mem_a else None, sums['mem_w'], sums['mem_c']),
        'pods': {'allocatable': sums['pod_a'] or None, 'workspace': sums['pod_w'], 'cluster': sums['pod_c']},
    }
    return nodes, cluster


# Bring the `node` label onto per-pod usage by joining on kube_pod_info. The
# inner expression already reduces to one value per (namespace, pod, container);
# group_left keeps that many-to-one (many containers per pod) valid.
def _per_node_usage(usage_inner, pod_filter=''):
    return (f'sum by (node) (({usage_inner}) '
            f'* on (namespace, pod) group_left (node) '
            f'(avg by (namespace, pod, node) (kube_pod_info{{{pod_filter}}})))')


def cluster_capacity(range_seconds=3600, step=300):
    """Cluster/node capacity rollup: per-node allocatable vs workspace vs total
    usage, the cluster totals, and cluster-level history so spikes are visible.

    Prometheus failures land in `metricsError` (like workspace_metrics) so a
    transient outage degrades the panel rather than 500-ing the request."""
    out = {
        'generatedAt': int(time.time()),
        'namespace': NAMESPACE,
        'cluster': None,
        'nodes': [],
        'history': {
            'rangeSeconds': range_seconds, 'step': step,
            'cpu': {'allocatable': [], 'workspace': [], 'cluster': []},
            'memory': {'allocatable': [], 'workspace': [], 'cluster': []},
        },
        'metricsError': None,
    }

    # Workspace band spans every per-user namespace (#103), not one shared ns.
    ws_sel = f'{_ws_prom_ns_selector()},pod=~"ws-.*"'
    ws_cpu_inner = f'avg by (namespace, pod, container) (rate(container_cpu_usage_seconds_total{{{ws_sel},container!=""}}[5m]))'
    ws_mem_inner = f'avg by (namespace, pod, container) (container_memory_working_set_bytes{{{ws_sel},container!=""}})'
    all_cpu_inner = 'avg by (namespace, pod, container) (rate(container_cpu_usage_seconds_total{container!="",pod!=""}[5m]))'
    all_mem_inner = 'avg by (namespace, pod, container) (container_memory_working_set_bytes{container!="",pod!=""})'
    alloc_cpu_total = 'sum(avg by (node) (kube_node_status_allocatable{resource="cpu"}))'
    alloc_mem_total = 'sum(avg by (node) (kube_node_status_allocatable{resource="memory"}))'

    def node_map(expr):
        return {lbl.get('node', ''): val for lbl, val in prom_instant_multi(expr) if lbl.get('node')}

    try:
        nodes, cluster = _node_rollup(
            node_map('avg by (node) (kube_node_status_allocatable{resource="cpu"})'),
            node_map('avg by (node) (kube_node_status_allocatable{resource="memory"})'),
            node_map('avg by (node) (kube_node_status_allocatable{resource="pods"})'),
            node_map(_per_node_usage(ws_cpu_inner, ws_sel)),
            node_map(_per_node_usage(ws_mem_inner, ws_sel)),
            node_map(_per_node_usage(all_cpu_inner)),
            node_map(_per_node_usage(all_mem_inner)),
            node_map(f'count by (node) (kube_pod_info{{{ws_sel}}})'),
            node_map('count by (node) (kube_pod_info)'),
        )
        out['nodes'] = nodes
        out['cluster'] = cluster
        out['history']['cpu']['allocatable'] = prom_range(alloc_cpu_total, range_seconds, step)
        out['history']['cpu']['workspace'] = prom_range(f'sum({ws_cpu_inner})', range_seconds, step)
        out['history']['cpu']['cluster'] = prom_range(f'sum({all_cpu_inner})', range_seconds, step)
        out['history']['memory']['allocatable'] = prom_range(alloc_mem_total, range_seconds, step)
        out['history']['memory']['workspace'] = prom_range(f'sum({ws_mem_inner})', range_seconds, step)
        out['history']['memory']['cluster'] = prom_range(f'sum({all_mem_inner})', range_seconds, step)
    except PromError as exc:
        out['metricsError'] = str(exc)
    return out


def _health_status(*blocks):
    """Overall traffic-light from the worst cluster-usage percentage across the
    given rollups: ok < 75% <= warn < 90% <= crit. 'unknown' when no rollup has
    a percentage (allocatable missing)."""
    worst = None
    for b in blocks:
        p = (b or {}).get('clusterPct')
        if p is not None:
            worst = p if worst is None else max(worst, p)
    if worst is None:
        return 'unknown'
    if worst >= 90:
        return 'crit'
    if worst >= 75:
        return 'warn'
    return 'ok'


def cluster_health():
    """Cheap cluster-health summary for the dashboard landing page: cluster CPU +
    memory rollups and an overall traffic-light status, from a handful of INSTANT
    Prometheus queries.

    Deliberately does NOT run the per-node breakdown or the range-history queries
    that cluster_capacity does — those are the memory/latency-heavy part and now
    load only on the /capacity drill-down. Keeping the landing page down to these
    ~7 instant scalars is what stops the burst of heavy queries (and the OOM/502s)
    on every dashboard load. Prometheus failures degrade to metricsError."""
    out = {
        'generatedAt': int(time.time()),
        'namespace': NAMESPACE,
        'cluster': None,
        'status': 'unknown',
        'metricsError': None,
    }
    ws_sel = f'{_ws_prom_ns_selector()},pod=~"ws-.*"'
    ws_cpu_inner = f'avg by (namespace, pod, container) (rate(container_cpu_usage_seconds_total{{{ws_sel},container!=""}}[5m]))'
    ws_mem_inner = f'avg by (namespace, pod, container) (container_memory_working_set_bytes{{{ws_sel},container!=""}})'
    all_cpu_inner = 'avg by (namespace, pod, container) (rate(container_cpu_usage_seconds_total{container!="",pod!=""}[5m]))'
    all_mem_inner = 'avg by (namespace, pod, container) (container_memory_working_set_bytes{container!="",pod!=""})'
    try:
        alloc_cpu = prom_scalar('sum(avg by (node) (kube_node_status_allocatable{resource="cpu"}))')
        alloc_mem = prom_scalar('sum(avg by (node) (kube_node_status_allocatable{resource="memory"}))')
        node_count = prom_scalar('count(count by (node) (kube_node_status_allocatable))')
        cpu = _resource_block(alloc_cpu, prom_scalar(f'sum({ws_cpu_inner})') or 0.0,
                              prom_scalar(f'sum({all_cpu_inner})') or 0.0)
        mem = _resource_block(alloc_mem, prom_scalar(f'sum({ws_mem_inner})') or 0.0,
                              prom_scalar(f'sum({all_mem_inner})') or 0.0)
        out['cluster'] = {'nodeCount': int(node_count or 0), 'cpu': cpu, 'memory': mem}
        out['status'] = _health_status(cpu, mem)
    except PromError as exc:
        out['metricsError'] = str(exc)
    return out


# --- Provisioning ------------------------------------------------------------
#
# Self-service onboarding: an admin types a GitHub username, creates a GitHub
# OAuth App by hand (callback https://<slug>.<domain>/oauth2/callback) and pastes
# its Client ID + Secret into the console. The controller then (1) writes the
# rendered workspace values + secrets to a private GitOps repo and (2) launches a
# short-lived, privileged Job that runs `helm upgrade`. The always-on controller
# never holds write power over workspaces: it only validates input, pushes to
# git, and creates the Job.
#
# It MUST be an OAuth App, not a GitHub App: oauth2-proxy --provider=github drives
# the OAuth-App web flow, and GitHub returns its 404 page when the authorize URL
# carries a GitHub-App client id (the `Iv…` prefix). OAuth App client ids start
# with `Ov…` (or are 20-hex on legacy apps); validate_oauth_creds rejects the
# `Iv…` mistake. GitHub exposes no API to create classic OAuth Apps — that is the
# whole reason this one step is manual rather than a button.

# New workspaces are created at <login>.<domain> (e.g. dev.scalebase.io). The
# wildcard *.<domain> already resolves to the ingress, so no DNS step is needed.
WORKSPACE_DOMAIN = os.environ.get('WORKSPACE_DOMAIN', '').strip()
# Private repo holding generated values+secrets, host/path only (no scheme), e.g.
# github.com/imran31415/kube-coder-users.git. The controller pushes; the Job clones.
GITOPS_REPO = os.environ.get('GITOPS_REPO', '').strip()
GITOPS_BRANCH = os.environ.get('GITOPS_BRANCH', 'main').strip()
# Token (GitHub App installation token or PAT) with push access to GITOPS_REPO
# and read on the GitHub API. Injected from a Secret; empty => provisioning off.
GITOPS_TOKEN = os.environ.get('GITOPS_TOKEN', '').strip()
# Repo + ref the provisioner Job pulls the workspace Helm chart from.
CHART_REPO = os.environ.get('CHART_REPO', 'https://github.com/imran31415/kube-coder.git').strip()
CHART_REF = os.environ.get('CHART_REF', 'main').strip()
# Image the provisioner Job runs (needs git+make+kubectl; installs helm on the
# fly if absent). Defaults to the controller's own image.
PROVISIONER_IMAGE = os.environ.get('PROVISIONER_IMAGE', '').strip()
PROVISIONER_SA = os.environ.get('PROVISIONER_SERVICE_ACCOUNT', 'workspace-provisioner').strip()
PROVISIONER_PULL_SECRET = os.environ.get('PROVISIONER_PULL_SECRET', '').strip()
# Tag the new workspace runs; the chart prefixes it with `devlaptop-`.
WORKSPACE_IMAGE_TAG = os.environ.get('WORKSPACE_IMAGE_TAG', '').strip()
# Shared cluster Secret names projected into each provisioned workspace so the
# console path matches a hand-scaffolded one (scripts/user-template): the
# self-serve-update token (update.selfServeSecretName) and the shared OpenRouter
# key (assistant.openrouter.sharedSecretName). Blank => that feature stays off.
WORKSPACE_SELF_SERVE_SECRET = os.environ.get('WORKSPACE_SELF_SERVE_SECRET', '').strip()
WORKSPACE_ASSISTANT_SECRET = os.environ.get('WORKSPACE_ASSISTANT_SECRET', '').strip()
GITHUB_API = 'https://api.github.com'
GITHUB_TIMEOUT = int(os.environ.get('GITHUB_TIMEOUT', '15'))
# GitHub login: alphanumeric or single hyphens, max 39. Lowercased it is always
# a valid DNS label (used for ws-<slug> and <slug>.<domain>).
_GH_LOGIN_RE = re.compile(r'^[a-zA-Z0-9](?:-?[a-zA-Z0-9]){0,38}$')


def provisioning_enabled():
    """All the wiring a real provision needs; if any is missing we 501 cleanly
    rather than half-run. Both the git push and the GitHub user lookup need the
    token; the OAuth App creds come from the admin per request, not from env."""
    return bool(WORKSPACE_DOMAIN and GITOPS_REPO and GITOPS_TOKEN)


class GithubError(RuntimeError):
    def __init__(self, message, status=0, detail=''):
        super().__init__(message)
        self.status = status
        self.detail = detail


class ProvisionError(RuntimeError):
    pass


def _github_api(method, path, token=None, body=None):
    """Minimal GitHub REST call (stdlib only). Raises GithubError on failure."""
    url = path if path.startswith('http') else f'{GITHUB_API}{path}'
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Accept', 'application/vnd.github+json')
    req.add_header('User-Agent', 'workspace-controller')
    req.add_header('X-GitHub-Api-Version', '2022-11-28')
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=GITHUB_TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', 'replace')[:500]
        raise GithubError(f'github {method} {path} -> HTTP {exc.code}', exc.code, detail)
    except (OSError, ValueError) as exc:  # URLError/timeout subclass OSError
        raise GithubError(f'github {method} {path} failed: {exc}')


def slugify(login):
    """Workspace slug from a GitHub login: lowercased. GitHub logins are already
    DNS-label-safe (alphanumeric + single hyphens), so this is all that's needed."""
    return login.lower()


def validate_github_user(login):
    """Confirm the login is a real GitHub user account and pull display fields."""
    if not _GH_LOGIN_RE.match(login):
        raise ValueError('invalid github username')
    u = _github_api('GET', f'/users/{login}', token=GITOPS_TOKEN or None)
    if u.get('type') != 'User':
        raise ValueError(f'{login} is a {u.get("type", "non-user")}, not a user account')
    slug = slugify(u['login'])
    return {
        'login': u['login'],
        'slug': slug,
        'name': u.get('name') or u['login'],
        'email': u.get('email') or '',
        'avatarUrl': u.get('avatar_url'),
        'host': f'{slug}.{WORKSPACE_DOMAIN}',
        'exists': workspace_exists(slug),
        # True if the OAuth App creds + config were already pushed to the GitOps
        # repo on a previous attempt. Lets the UI skip re-entering the creds and
        # deploy straight from the saved config.
        'configExists': gitops_config_exists(slug),
    }


def workspace_exists(slug):
    # A workspace lives in its own ws-<slug> namespace (#103); check there first,
    # then the control-plane namespace for one not yet migrated. Used to block
    # double-provisioning, so a match in EITHER namespace counts as "exists".
    name = f'{WORKSPACE_PREFIX}{slug}'
    for ns in (ns_for_user(slug), NAMESPACE):
        try:
            _kubectl_json(['get', f'deployment/{name}'], namespace=ns)
            return True
        except KubectlError:
            continue
    return False


def oauth_callback_url(host):
    """The Authorization callback URL the admin must register on the OAuth App —
    exactly what oauth2-proxy expects for this workspace's host."""
    return f'https://{host}/oauth2/callback'


def validate_oauth_creds(client_id, client_secret):
    """Sanity-check a pasted GitHub OAuth App Client ID + Secret. We can't fully
    verify them without running the OAuth dance, but we can reject the blanks and
    the one mistake that silently breaks login: a GitHub *App* client id (the
    `Iv…` prefix), which 404s oauth2-proxy's --provider=github. Real OAuth App
    ids start with `Ov…` (or are 20-hex on legacy apps). Returns the trimmed pair."""
    cid = (client_id or '').strip()
    secret = (client_secret or '').strip()
    if not cid or not secret:
        raise ValueError('GitHub OAuth App Client ID and Client Secret are both required')
    if cid.lower().startswith('iv'):
        raise ValueError('that is a GitHub App Client ID (starts with "Iv…") — create an '
                         'OAuth App instead (Settings → Developer settings → OAuth Apps); '
                         'its Client ID starts with "Ov…".')
    return cid, secret


def gen_cookie_secret():
    """32 alphanumeric chars for oauth2-proxy's AES cookie key, matching the
    shape scripts/new-user.sh generates with openssl."""
    pool = ''.join(c for c in base64.b64encode(os.urandom(64)).decode() if c.isalnum())
    if len(pool) < 32:                       # astronomically unlikely; retry to be safe
        return gen_cookie_secret()
    return pool[:32]


def render_values_yaml(opts, client_id, cookie_secret):
    """Render the workspace values.yaml. Mirrors scripts/user-template/
    values.yaml.tmpl so a provisioned workspace is byte-compatible with a
    hand-scaffolded one — kept here (not read from disk) because the controller
    image ships only this file via ConfigMap."""
    slug = opts['slug']
    host = opts['host']
    # Default new workspaces to the latest published release so they aren't
    # pinned to a stale version at create time. Precedence: an explicit
    # per-request imageTag wins (admin pinned a specific version); otherwise the
    # latest release; WORKSPACE_IMAGE_TAG is only a fallback for when the release
    # lookup is unavailable (e.g. GitHub unreachable), and 'v1.0.0' a last resort.
    tag = opts.get('imageTag') or latest_version() or WORKSPACE_IMAGE_TAG or 'v1.0.0'
    pvc = opts.get('pvcSize') or '20Gi'
    res = opts.get('resources') or {}
    req_cpu = (res.get('requests') or {}).get('cpu', '250m')
    req_mem = (res.get('requests') or {}).get('memory', '1Gi')
    lim_cpu = (res.get('limits') or {}).get('cpu', '2')
    lim_mem = (res.get('limits') or {}).get('memory', '4Gi')
    git_name = opts.get('gitName') or opts['login']
    git_email = opts.get('gitEmail') or f'{opts["login"]}@users.noreply.github.com'
    # github-user allowlist is the access gate: only this login may sign in.
    # Per-workspace namespace (#103): the workspace lands in its OWN ws-<slug>
    # namespace, and `controller.namespace` / `update.controllerNamespace` point
    # back at the control-plane namespace the controller runs in so its RoleBinding
    # subject + self-serve URL resolve correctly across namespaces.
    return f"""# Workspace values for {slug} — generated by workspace-controller provisioning.
namespace: {ns_for_user(slug)}

controller:
  namespace: {NAMESPACE}

user:
  name: {slug}
  pvcSize: {pvc}
  host: {host}
  env:
    - name: GIT_USER_NAME
      value: {json.dumps(git_name)}
    - name: GIT_USER_EMAIL
      value: {json.dumps(git_email)}

image:
  repository: registry.digitalocean.com/resourceloop/coder
  tag: devlaptop-{tag}
  pullPolicy: Always
  pullSecretName: regcred

ingress:
  className: nginx
  auth:
    type: oauth2
    secretName: api-basic-auth
  tls:
    enabled: true
    secretName: {slug}-{WORKSPACE_DOMAIN.replace('.', '-')}-tls
    clusterIssuer: letsencrypt-production

oauth2:
  githubUsers: {json.dumps(opts['login'])}
  cookieSecret: {json.dumps(cookie_secret)}
  clientId: {json.dumps(client_id)}
  clientSecret: "OVERRIDE-IN-SECRETS-OAUTH2-YAML"

resources:
  requests:
    cpu: {json.dumps(req_cpu)}
    memory: {json.dumps(req_mem)}
  limits:
    cpu: {json.dumps(lim_cpu)}
    memory: {json.dumps(lim_mem)}

build:
  mode: buildkit
  kanikoImage: gcr.io/kaniko-project/executor:latest
  pushSecretName: regcred
  defaultDestinationRepo: registry.digitalocean.com/resourceloop/coder

ssh:
  enabled: false
  port: 22

claude:
  apiKey: ""

assistant:
  openrouter:
    sharedSecretName: {json.dumps(WORKSPACE_ASSISTANT_SECRET)}

update:
  selfServeSecretName: {json.dumps(WORKSPACE_SELF_SERVE_SECRET)}
  controllerNamespace: {NAMESPACE}

github:
  app:
    appId: ""
    installationId: ""
    privateKey: ""
"""


def render_oauth_secret_yaml(client_secret):
    """The one secret value, split out so values.yaml stays secret-free (mirrors
    the users-private/<u>/secrets/oauth2.yaml convention)."""
    return f"""# Generated by workspace-controller — do not edit by hand.
oauth2:
  clientSecret: {json.dumps(client_secret)}
"""


def gitops_publish(opts, client_id, client_secret, cookie_secret):
    """Clone the private GitOps repo, write users-private/<slug>/{values,secrets},
    commit and push. Returns the values+secret file paths (relative) the Job reads."""
    slug = opts['slug']
    values_yaml = render_values_yaml(opts, client_id, cookie_secret)
    oauth_yaml = render_oauth_secret_yaml(client_secret)
    workdir = tempfile.mkdtemp(prefix='gitops-', dir='/tmp')
    try:
        # x-access-token:<token> is GitHub's documented Basic-auth form for App
        # installation tokens and PATs alike. Confined to this pod's process table.
        repo_url = f'https://x-access-token:{GITOPS_TOKEN}@{GITOPS_REPO}'
        _git(['clone', '--depth', '1', '-b', GITOPS_BRANCH, repo_url, workdir])
        udir = os.path.join(workdir, 'users-private', slug)
        os.makedirs(os.path.join(udir, 'secrets'), exist_ok=True)
        with open(os.path.join(udir, 'values.yaml'), 'w') as f:
            f.write(values_yaml)
        with open(os.path.join(udir, 'secrets', 'oauth2.yaml'), 'w') as f:
            f.write(oauth_yaml)
        _git(['-C', workdir, 'add', '-A'])
        # Nothing to commit if re-provisioning identical config — treat as success.
        status = _git(['-C', workdir, 'status', '--porcelain'])
        if status.strip():
            _git(['-C', workdir,
                  '-c', 'user.email=controller@kube-coder',
                  '-c', 'user.name=workspace-controller',
                  'commit', '-m', f'provision: {slug}'])
            _git(['-C', workdir, 'push', 'origin', GITOPS_BRANCH])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _gitops_owner_repo():
    """`owner/repo` from GITOPS_REPO (`host/owner/repo.git`, no scheme)."""
    p = GITOPS_REPO.split('/', 1)[1] if '/' in GITOPS_REPO else GITOPS_REPO
    return p[:-4] if p.endswith('.git') else p


def gitops_config_exists(slug):
    """True if the GitOps repo already holds rendered config for this slug — i.e.
    the OAuth creds were saved on a previous attempt. A retry can then skip
    re-entering the creds and deploy straight from the saved config. Defensive:
    any lookup failure returns False, falling back to the normal create flow."""
    if not (GITOPS_REPO and GITOPS_TOKEN):
        return False
    path = f'/repos/{_gitops_owner_repo()}/contents/users-private/{slug}/values.yaml?ref={GITOPS_BRANCH}'
    try:
        _github_api('GET', path, token=GITOPS_TOKEN)
        return True
    except GithubError as exc:
        if getattr(exc, 'status', None) != 404:
            sys.stderr.write(f'[controller] gitops_config_exists({slug}) check failed: {exc}\n')
        return False


def _redact(text):
    """Strip the git token from any text before it reaches a client or log —
    git error output echoes the token-bearing clone URL on failure."""
    if GITOPS_TOKEN and text:
        text = text.replace(GITOPS_TOKEN, '***')
    return text


def _git(args):
    proc = subprocess.run(['git', *args], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise ProvisionError(f'git {args[0]} failed: {_redact(proc.stderr.strip())[:300]}')
    return proc.stdout


# The Job clones the chart repo + the private config repo, assembles the
# users-private dir the Makefile expects, ensures helm is on PATH, then runs the
# same `make deploy` an operator would. Everything privileged lives here, not in
# the controller.
PROVISION_JOB_SCRIPT = r"""
set -euo pipefail
export HOME=/tmp
mkdir -p /tmp/bin
export PATH="/tmp/bin:$PATH"
if ! command -v helm >/dev/null 2>&1; then
  echo "helm not found — installing to /tmp/bin"
  curl -fsSL https://get.helm.sh/helm-v3.14.4-linux-amd64.tar.gz | tar -xz -C /tmp
  install -m 0755 /tmp/linux-amd64/helm /tmp/bin/helm
fi
git clone --depth 1 -b "$CHART_REF" "$CHART_REPO" /tmp/kc
git clone --depth 1 -b "$GITOPS_BRANCH" "https://x-access-token:${GITOPS_TOKEN}@${GITOPS_REPO}" /tmp/cfg
mkdir -p /tmp/kc/users-private
cp -r "/tmp/cfg/users-private/${SLUG}" "/tmp/kc/users-private/${SLUG}"
cd /tmp/kc
# Per-workspace namespace (#103): deploy into ws-<slug>, and copy the regcred
# image-pull Secret from the control-plane namespace into it. `make deploy`
# creates+labels the namespace and replicates regcred (see REGCRED_SRC_NAMESPACE).
make deploy USER="${SLUG}" NAMESPACE="${WS_NAMESPACE}" REGCRED_SRC_NAMESPACE="${NAMESPACE}"
"""


def build_job_manifest(slug):
    name = f'provision-{slug}-{int(time.time())}'[:63]
    image = PROVISIONER_IMAGE or f'{os.environ.get("CONTROLLER_IMAGE", "")}' or ''
    env = [
        {'name': 'SLUG', 'value': slug},
        # NAMESPACE = the control-plane namespace the Job runs in (regcred source);
        # WS_NAMESPACE = the workspace's own per-user namespace it deploys into (#103).
        {'name': 'NAMESPACE', 'value': NAMESPACE},
        {'name': 'WS_NAMESPACE', 'value': ns_for_user(slug)},
        {'name': 'CHART_REPO', 'value': CHART_REPO},
        {'name': 'CHART_REF', 'value': CHART_REF},
        {'name': 'GITOPS_REPO', 'value': GITOPS_REPO},
        {'name': 'GITOPS_BRANCH', 'value': GITOPS_BRANCH},
        {'name': 'GITOPS_TOKEN', 'value': GITOPS_TOKEN},
    ]
    container = {
        'name': 'provision',
        'image': image,
        'command': ['bash', '-c', PROVISION_JOB_SCRIPT],
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
    return {
        'apiVersion': 'batch/v1',
        'kind': 'Job',
        'metadata': {
            'name': name,
            'namespace': NAMESPACE,
            'labels': {'app': 'workspace-provisioner', 'provisionUser': slug},
        },
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


def _kubectl_apply(manifest):
    cmd = ['kubectl', 'apply', '-n', NAMESPACE, '-f', '-']
    proc = subprocess.run(cmd, input=json.dumps(manifest), capture_output=True,
                          text=True, timeout=KUBECTL_TIMEOUT)
    if proc.returncode != 0:
        raise KubectlError('kubectl apply failed', proc.stderr.strip())
    return proc.stdout.strip()


def create_provision_job(slug):
    return _kubectl_apply(build_job_manifest(slug))


def provision_status(slug):
    """Latest provisioning Job for this user + the resulting workspace state."""
    jobs = _kubectl_json(['get', 'jobs', '-l', f'provisionUser={slug}']).get('items', [])
    job_state, message = 'none', ''
    if jobs:
        jobs.sort(key=lambda j: j.get('metadata', {}).get('creationTimestamp', ''))
        st = jobs[-1].get('status', {})
        if st.get('succeeded'):
            job_state = 'succeeded'
        elif st.get('failed'):
            job_state, message = 'failed', 'provisioner Job failed — see Job logs'
        elif st.get('active'):
            job_state = 'running'
        else:
            job_state = 'pending'
    ws = next((w for w in list_workspaces()['workspaces'] if w['user'] == slug), None)
    return {
        'slug': slug,
        'job': job_state,
        'message': message,
        'workspace': ws,
        'url': ws['url'] if ws else f'https://{slug}.{WORKSPACE_DOMAIN}/',
    }


def provision_workspace(login, client_id, client_secret, opts_in):
    """Validate the user + the operator-supplied OAuth App creds, render+push the
    workspace config to the GitOps repo, and launch the deploy Job. Returns the
    slug. Raises ValueError on bad input, GithubError on the user lookup."""
    info = validate_github_user(login)            # confirms a real user, derives slug/host
    cid, secret = validate_oauth_creds(client_id, client_secret)
    opts = {
        'login': info['login'],
        'slug': info['slug'],
        'host': info['host'],
        'pvcSize': opts_in.get('pvcSize'),
        'resources': opts_in.get('resources'),
        'gitName': opts_in.get('gitName') or info['name'],
        'gitEmail': opts_in.get('gitEmail') or info['email'],
        'imageTag': opts_in.get('imageTag'),
    }
    cookie_secret = gen_cookie_secret()
    gitops_publish(opts, cid, secret, cookie_secret)
    create_provision_job(opts['slug'])
    return opts['slug']


# --- HTTP handler ------------------------------------------------------------

class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = 'workspace-controller/0.1'

    def log_message(self, fmt, *args):
        sys.stderr.write('[controller] %s - %s\n' % (self.address_string(), fmt % args))

    # ----- helpers (mirror server.py) -----

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The client (usually the ingress, occasionally a navigated-away
            # SPA) hung up before we finished writing. There's nothing to
            # recover, and letting it propagate crashes the handler thread with
            # a noisy traceback, so swallow it.
            pass

    def read_json_body(self):
        n = int(self.headers.get('Content-Length', 0))
        if n == 0:
            return {}
        if n > MAX_REQUEST_BODY_BYTES:
            raise ValueError('request body too large')
        raw = self.rfile.read(n).decode('utf-8')
        return json.loads(raw) if raw else {}

    def _bearer(self):
        """The Bearer credential from the Authorization header, or ''."""
        auth = self.headers.get('Authorization', '')
        return auth[7:].strip() if auth.startswith('Bearer ') else ''

    def check_admin(self, allow_token=True):
        """True if the request is from an allowed admin.

        oauth2-proxy is the primary gate; this is defense-in-depth. Two bearer
        tokens also grant access without proxy headers: the local-dev
        CONTROLLER_DEV_TOKEN, and the production ADMIN_TOKEN used by the mobile
        app. Both bypass the ADMIN_USERS allowlist — the token itself is the
        grant. `allow_token=False` restricts to the proxy/oauth path only, for
        endpoints that must be reachable *solely* by a signed-in browser admin
        (e.g. revealing the admin token itself).
        """
        if allow_token:
            tok = self._bearer()
            if tok:
                if DEV_TOKEN and hmac.compare_digest(tok, DEV_TOKEN):
                    return True
                if ADMIN_TOKEN and hmac.compare_digest(tok, ADMIN_TOKEN):
                    return True
        if TRUSTED_PROXY:
            # oauth2-proxy injects identity differently per mode: as a REVERSE
            # PROXY (our setup) --pass-user-headers sends X-Forwarded-User /
            # -Email / -Preferred-Username to the upstream; in nginx
            # auth_request (external-auth) mode it's X-Auth-Request-*. Accept
            # either so the same backend works behind both.
            user = (self.headers.get('X-Forwarded-Preferred-Username')
                    or self.headers.get('X-Forwarded-User')
                    or self.headers.get('X-Auth-Request-User')
                    or self.headers.get('Remote-User') or '')
            email = (self.headers.get('X-Forwarded-Email')
                     or self.headers.get('X-Auth-Request-Email') or '')
            if not (user or email):
                return False
            # If an allowlist is configured, require a username AND require
            # the username (not just email) to be on the list. Previously a
            # request with only X-Forwarded-Email fell through to "return
            # True" because the `and user` clause short-circuited — that
            # silently bypassed the allowlist when oauth2-proxy was
            # configured to forward email but not username.
            if ADMIN_USERS:
                if not user or user.lower() not in ADMIN_USERS:
                    return False
            return True
        return False

    def check_service_token(self):
        """True if the request carries the shared self-serve token.

        This is the auth for the in-cluster `/api/self/*` endpoints a
        workspace's own backend brokers to (bypassing the oauth2 admin gate via
        the controller's internal Service). Constant-time compare; disabled
        cleanly when SELF_SERVE_TOKEN is unset.
        """
        if not SELF_SERVE_TOKEN:
            return False
        tok = self.headers.get('X-KC-Service-Token', '')
        return bool(tok) and hmac.compare_digest(tok, SELF_SERVE_TOKEN)

    def _norm_path(self):
        """Strip query + the SPA's /oauth prefix; return the upstream path."""
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith('/oauth/'):
            path = path[len('/oauth'):]   # '/oauth/api/x' -> '/api/x'
        elif path == '/oauth':
            path = '/'
        return path

    # ----- routing -----

    def _restricted_block(self, path, allowed_re):
        """On the self-serve-only listener, 404 anything that isn't a health
        check or an allowed self-serve route. Returns True if it handled (and
        blocked) the request. Keeps the admin/header-trusting routes — which the
        same Handler implements — unreachable from workspace pods."""
        if not getattr(self.server, 'restricted', False):
            return False
        if path in ('/health', '/livez', '/healthz') or allowed_re.match(path):
            return False
        self.send_json({'error': 'not found'}, 404)
        return True

    def do_GET(self):
        path = self._norm_path()
        if self._restricted_block(path, _SELF_SERVE_GET_RE):
            return
        if path in ('/health', '/livez', '/healthz'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'ok')
            return
        if path == '/api/workspaces':
            if not self.check_admin():
                self.send_json({'error': 'unauthorized'}, 401)
                return
            try:
                self.send_json(decorate_with_updates(list_workspaces()))
            except KubectlError as exc:
                sys.stderr.write(f'[controller] {exc}: {exc.stderr}\n')
                self.send_json({'error': str(exc)}, 502)
            return
        if path == '/api/admin/token':
            # Reveal the persistent admin token so a signed-in admin can copy it
            # into the mobile app. allow_token=False: only a browser/oauth admin
            # may read it — a mobile client holding the token can't re-fetch it
            # (it already has it), and a no-proxy deploy exposes nothing. Returns
            # {enabled:false} cleanly when the token isn't configured so the web
            # console can hide the section instead of erroring.
            if not self.check_admin(allow_token=False):
                self.send_json({'error': 'unauthorized'}, 401)
                return
            self.send_json({'enabled': bool(ADMIN_TOKEN), 'token': ADMIN_TOKEN or None})
            return
        # Self-serve (workspace-brokered): current vs latest version for one
        # workspace, gated by the shared service token instead of admin.
        sv = re.match(r'^/api/self/workspaces/([a-z0-9-]{1,41})/version$', path)
        if sv:
            if not self.check_service_token():
                self.send_json({'error': 'unauthorized'}, 401)
                return
            user = sv.group(1)
            try:
                self.send_json(workspace_version_info(user))
            except ValueError as exc:
                self.send_json({'error': str(exc)}, 400)
            except LookupError:
                self.send_json({'error': f'no workspace {WORKSPACE_PREFIX}{user}'}, 404)
            except KubectlError as exc:
                sys.stderr.write(f'[controller] self version {user}: {exc}: {exc.stderr}\n')
                self.send_json({'error': str(exc)}, 502)
            return
        if path == '/api/insights':
            if not self.check_admin():
                self.send_json({'error': 'unauthorized'}, 401)
                return
            try:
                self.send_json(compute_insights())
            except KubectlError as exc:
                sys.stderr.write(f'[controller] {exc}: {exc.stderr}\n')
                self.send_json({'error': str(exc)}, 502)
            return
        if path == '/api/capacity/summary':
            if not self.check_admin():
                self.send_json({'error': 'unauthorized'}, 401)
                return
            # Cheap instant-only rollup for the landing page — no range history,
            # no per-node. Prometheus-only, so any outage lands in metricsError
            # and this still returns 200.
            self.send_json(cluster_health())
            return
        if path == '/api/capacity':
            if not self.check_admin():
                self.send_json({'error': 'unauthorized'}, 401)
                return
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                rng = int(q.get('range', ['3600'])[0])
            except ValueError:
                rng = 3600
            rng = max(300, min(rng, 2592000))   # 5 min .. 30 days, mirrors /metrics
            step = max(15, rng // 150)           # ~150 points at any range
            # Prometheus-only (no kubectl): any backend outage is captured in
            # the payload's metricsError, so this always returns 200.
            self.send_json(cluster_capacity(rng, step))
            return
        m = re.match(r'^/api/workspaces/([a-z0-9-]{1,41})/metrics$', path)
        if m:
            if not self.check_admin():
                self.send_json({'error': 'unauthorized'}, 401)
                return
            user = m.group(1)
            if not _USER_RE.match(user):
                self.send_json({'error': 'invalid workspace name'}, 400)
                return
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                rng = int(q.get('range', ['3600'])[0])
            except ValueError:
                rng = 3600
            rng = max(300, min(rng, 2592000))   # 5 min .. 30 days
            step = max(15, rng // 150)           # ~150 points at any range
            try:
                self.send_json(workspace_metrics(user, rng, step))
            except LookupError:
                self.send_json({'error': f'no workspace {WORKSPACE_PREFIX}{user}'}, 404)
            except KubectlError as exc:
                sys.stderr.write(f'[controller] {exc}: {exc.stderr}\n')
                self.send_json({'error': str(exc)}, 502)
            return
        if path == '/api/provision/config':
            if not self.check_admin():
                self.send_json({'error': 'unauthorized'}, 401)
                return
            self.send_json({
                'enabled': provisioning_enabled(),
                'workspaceDomain': WORKSPACE_DOMAIN,
                # Where the admin creates the OAuth App they'll paste creds from.
                'oauthAppNewUrl': 'https://github.com/settings/applications/new',
            })
            return
        if path == '/api/provision/validate':
            if not self.check_admin():
                self.send_json({'error': 'unauthorized'}, 401)
                return
            if not provisioning_enabled():
                self.send_json({'error': 'provisioning not configured'}, 501)
                return
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            login = (q.get('user', ['']) or [''])[0].strip()
            try:
                self.send_json(validate_github_user(login))
            except ValueError as exc:
                self.send_json({'error': str(exc)}, 400)
            except GithubError as exc:
                code = 404 if exc.status == 404 else 502
                self.send_json({'error': f'github lookup failed: {exc}'}, code)
            return
        m = re.match(r'^/api/provision/([a-z0-9-]{1,41})/status$', path)
        if m:
            if not self.check_admin():
                self.send_json({'error': 'unauthorized'}, 401)
                return
            if not provisioning_enabled():
                self.send_json({'error': 'provisioning not configured'}, 501)
                return
            try:
                self.send_json(provision_status(m.group(1)))
            except KubectlError as exc:
                self.send_json({'error': str(exc)}, 502)
            return
        if path.startswith('/api/'):
            self.send_json({'error': 'not found'}, 404)
            return
        self.serve_spa(path)

    def handle_provision_create(self):
        """Provision from operator-supplied OAuth App creds: validate the user +
        creds, push config to the GitOps repo, launch the deploy Job, and return
        the initial status so the SPA can switch straight to the live poller."""
        try:
            body = self.read_json_body()
        except ValueError:
            self.send_json({'error': 'invalid body'}, 400)
            return
        login = str(body.get('user', '')).strip()
        try:
            slug = provision_workspace(login, body.get('clientId'), body.get('clientSecret'), body)
            self.send_json(provision_status(slug))
        except ValueError as exc:
            self.send_json({'error': str(exc)}, 400)
        except GithubError as exc:
            self.send_json({'error': f'github lookup failed: {exc}'}, 502)
        except (ProvisionError, KubectlError) as exc:
            sys.stderr.write(f'[controller] provision failed: {exc}\n')
            self.send_json({'error': str(exc)[:200]}, 502)

    def do_POST(self):
        path = self._norm_path()
        if self._restricted_block(path, _SELF_SERVE_POST_RE):
            return
        if path == '/api/provision/create':
            if not self.check_admin():
                self.send_json({'error': 'unauthorized'}, 401)
                return
            if not provisioning_enabled():
                self.send_json({'error': 'provisioning not configured'}, 501)
                return
            self.handle_provision_create()
            return
        dm = re.match(r'^/api/provision/([a-z0-9-]{1,41})/deploy$', path)
        if dm:
            # Idempotent re-deploy: the OAuth creds + config already exist in the
            # GitOps repo (e.g. a retry after a failed Job), so skip the create
            # form entirely and just relaunch the Job against the saved config.
            if not self.check_admin():
                self.send_json({'error': 'unauthorized'}, 401)
                return
            if not provisioning_enabled():
                self.send_json({'error': 'provisioning not configured'}, 501)
                return
            slug = dm.group(1)
            if not gitops_config_exists(slug):
                self.send_json({'error': f'no saved config for {slug} in the GitOps repo — create the workspace with its OAuth App creds first'}, 409)
                return
            try:
                create_provision_job(slug)
                self.send_json(provision_status(slug))
            except KubectlError as exc:
                self.send_json({'error': str(exc)}, 502)
            return
        rm = re.match(r'^/api/workspaces/([a-z0-9-]{1,41})/resources$', path)
        if rm:
            if not self.check_admin():
                self.send_json({'error': 'unauthorized'}, 401)
                return
            user = rm.group(1)
            try:
                body = self.read_json_body()
                result = set_workspace_resources(user, body.get('cpu'), body.get('memory'))
            except ValueError as exc:
                self.send_json({'error': str(exc)}, 400)
                return
            except LookupError:
                self.send_json({'error': f'no workspace {WORKSPACE_PREFIX}{user}'}, 404)
                return
            except KubectlError as exc:
                sys.stderr.write(f'[controller] set resources {user}: {exc}: {exc.stderr}\n')
                self.send_json({'error': str(exc)}, 502)
                return
            self.send_json({'ok': True, 'user': user, 'limits': result['limits'],
                            'persisted': result['persisted'], 'persistError': result['persistError']})
            return
        # Restart-and-update: repoint the workspace at a release version (latest
        # by default). Admin route (oauth2-gated) and the workspace-brokered
        # self-serve route (shared-token-gated) share one handler.
        um = re.match(r'^/api/workspaces/([a-z0-9-]{1,41})/update$', path)
        sm = re.match(r'^/api/self/workspaces/([a-z0-9-]{1,41})/update$', path)
        if um or sm:
            if um and not self.check_admin():
                self.send_json({'error': 'unauthorized'}, 401)
                return
            if sm and not self.check_service_token():
                self.send_json({'error': 'unauthorized'}, 401)
                return
            user = (um or sm).group(1)
            try:
                body = self.read_json_body()
                result = set_workspace_image(user, body.get('version'))
            except ValueError as exc:
                self.send_json({'error': str(exc)}, 400)
                return
            except LookupError:
                self.send_json({'error': f'no workspace {WORKSPACE_PREFIX}{user}'}, 404)
                return
            except KubectlError as exc:
                sys.stderr.write(f'[controller] update {user}: {exc}: {exc.stderr}\n')
                self.send_json({'error': str(exc)}, 502)
                return
            self.send_json({'ok': True, **result})
            return
        m = re.match(r'^/api/workspaces/([a-z0-9-]{1,41})/(start|stop)$', path)
        if not m:
            self.send_json({'error': 'not found'}, 404)
            return
        if not self.check_admin():
            self.send_json({'error': 'unauthorized'}, 401)
            return
        user, action = m.group(1), m.group(2)
        replicas = 1 if action == 'start' else 0
        try:
            self.read_json_body()  # drain body if any; we don't need it
            scale_workspace(user, replicas)
        except ValueError:
            self.send_json({'error': 'invalid workspace name'}, 400)
            return
        except LookupError:
            self.send_json({'error': f'no workspace {WORKSPACE_PREFIX}{user}'}, 404)
            return
        except KubectlError as exc:
            sys.stderr.write(f'[controller] scale {user}={replicas}: {exc}: {exc.stderr}\n')
            self.send_json({'error': str(exc)}, 502)
            return
        self.send_json({'ok': True, 'user': user, 'desiredReplicas': replicas})

    # ----- SPA serving (mirrors server.py:serve_next_spa) -----

    def serve_spa(self, path):
        if not os.path.isdir(DIST_DIR):
            self.send_error(404, 'SPA not built. Run `make controller-web` or set CONTROLLER_DIST_DIR.')
            return
        rel = urllib.parse.unquote(path).lstrip('/')
        if rel == '' or rel.endswith('/'):
            rel = 'index.html'
        base_real = os.path.realpath(DIST_DIR)
        target_real = os.path.realpath(os.path.join(base_real, rel))
        if not (target_real == base_real or target_real.startswith(base_real + os.sep)):
            self.send_error(403, 'Forbidden')
            return
        # History fallback: unknown extensionless path -> index.html (SPA).
        if not os.path.isfile(target_real) and '.' not in os.path.basename(target_real):
            target_real = os.path.join(base_real, 'index.html')
            rel = 'index.html'
        if not os.path.isfile(target_real):
            self.send_error(404, 'Not found')
            return
        ctype, _ = mimetypes.guess_type(target_real)
        with open(target_real, 'rb') as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype or 'application/octet-stream')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache, must-revalidate')
        self.end_headers()
        self.wfile.write(body)


def main():
    print(f'[controller] namespace={NAMESPACE} prefix={WORKSPACE_PREFIX!r} '
          f'trusted_proxy={TRUSTED_PROXY} admins={sorted(ADMIN_USERS) or "(proxy-gated)"}',
          file=sys.stderr)
    if DEV_TOKEN:
        print('[controller] WARNING: CONTROLLER_DEV_TOKEN set — bearer-token auth bypass '
              'is enabled. Local dev only.', file=sys.stderr)
    # Restricted self-serve listener (token-gated, no header trust) on a
    # separate port so workspace pods can self-update without ever being able
    # to reach the header-trusting admin API. Only started when a token is set.
    if SELF_SERVE_TOKEN:
        import threading
        self_httpd = http.server.ThreadingHTTPServer(('0.0.0.0', SELF_SERVE_PORT), Handler)
        self_httpd.restricted = True
        t = threading.Thread(target=self_httpd.serve_forever, daemon=True)
        t.start()
        print(f'[controller] self-serve listening on 0.0.0.0:{SELF_SERVE_PORT} (token-gated)',
              file=sys.stderr)
    else:
        print('[controller] self-serve disabled (SELF_SERVE_TOKEN unset)', file=sys.stderr)

    httpd = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f'[controller] listening on 0.0.0.0:{PORT}', file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == '__main__':
    main()
