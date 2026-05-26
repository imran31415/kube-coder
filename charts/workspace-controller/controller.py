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
import http.server
import hmac
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
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
DIST_DIR = os.environ.get('CONTROLLER_DIST_DIR', '/controller-web')
KUBECTL_TIMEOUT = int(os.environ.get('KUBECTL_TIMEOUT', '15'))
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
# DNS-label-ish. This is also the canonical "is this a workspace" test.
_NAME_RE = re.compile(r'^' + re.escape(WORKSPACE_PREFIX) + r'([a-z0-9][a-z0-9-]{0,40})$')
_USER_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,40}$')


class KubectlError(RuntimeError):
    def __init__(self, message, stderr=''):
        super().__init__(message)
        self.stderr = stderr


def _kubectl_json(args, _attempts=2):
    """Run `kubectl <args> -o json -n <ns>` and parse stdout.

    Retries once on failure: kubectl's discovery cache is cold right after a pod
    start and several read endpoints fire concurrently on page load, which can
    make the first call fail transiently. These are read-only, so a retry is safe."""
    cmd = ['kubectl', *args, '-n', NAMESPACE, '-o', 'json']
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


def _kubectl_run(args):
    """Run a mutating kubectl command; raise KubectlError on failure."""
    cmd = ['kubectl', *args, '-n', NAMESPACE]
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


def list_workspaces():
    deps = _kubectl_json(['get', 'deployments']).get('items', [])
    all_pods = _kubectl_json(['get', 'pods']).get('items', [])
    # Hosts are best-effort; if the RBAC for ingresses is absent or the call
    # fails we still return the list, just without clickable URLs.
    hosts = {}
    try:
        for ing in _kubectl_json(['get', 'ingress']).get('items', []):
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
        out.append({
            'user': user,
            'deployment': name,
            'state': state,
            'desiredReplicas': desired,
            'readyReplicas': ready,
            'url': f'https://{host}/' if host else None,
            'pods': pods,
            'detail': detail,
        })
    out.sort(key=lambda w: w['user'])
    return {'namespace': NAMESPACE, 'workspaces': out}


def scale_workspace(user, replicas):
    if not _USER_RE.match(user):
        raise ValueError('invalid workspace name')
    name = f'{WORKSPACE_PREFIX}{user}'
    # Confirm the deployment exists (and is actually a workspace) before
    # touching it — never scale something the listing wouldn't show.
    existing = {w['deployment'] for w in list_workspaces()['workspaces']}
    if name not in existing:
        raise LookupError(name)
    _kubectl_run(['scale', f'deployment/{name}', f'--replicas={replicas}'])
    return name


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
    try:
        dep = _kubectl_json(['get', f'deployment/{name}'])
    except KubectlError as exc:
        raise LookupError(name) from exc

    spec = dep.get('spec', {})
    desired = spec.get('replicas', 1)
    containers = spec.get('template', {}).get('spec', {}).get('containers', [])
    cpu_limit = sum(c for c in (parse_cpu((cn.get('resources', {}).get('limits', {}) or {}).get('cpu'))
                                for cn in containers) if c) or None
    mem_limit = sum(c for c in (parse_bytes((cn.get('resources', {}).get('limits', {}) or {}).get('memory'))
                                for cn in containers) if c) or None

    pvc_bytes = None
    try:
        pvc = _kubectl_json(['get', f'pvc/{name}-home'])
        cap = ((pvc.get('status', {}).get('capacity', {}) or {}).get('storage')
               or (pvc.get('spec', {}).get('resources', {}).get('requests', {}) or {}).get('storage'))
        pvc_bytes = parse_bytes(cap)
    except KubectlError:
        pass

    pod_re = f'{name}-.*'
    ns = NAMESPACE
    out = {
        'user': user,
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
    covered = min(window, f['uptime']) if f['uptime'] else window
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
        deps = _kubectl_json(['get', 'deployments']).get('items', [])
        pods = _kubectl_json(['get', 'pods']).get('items', [])
        pvcs = _kubectl_json(['get', 'pvc']).get('items', [])
    except KubectlError as exc:
        out['error'] = str(exc)
        return out

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

    import datetime
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

    ns = NAMESPACE
    cpu_by_user, mem_by_user, disk_by_user = {}, {}, {}
    try:
        for metric, vals in prom_range_multi(
                f'sum by (pod) (avg by (pod, container) (rate(container_cpu_usage_seconds_total{{namespace="{ns}",pod=~"ws-.*",container!=""}}[5m])))',
                window, step):
            u = _user_for_pod(metric.get('pod', ''), names)
            if u:
                cpu_by_user[u] = vals
        for metric, vals in prom_range_multi(
                f'sum by (pod) (avg by (pod, container) (container_memory_working_set_bytes{{namespace="{ns}",pod=~"ws-.*",container!=""}}))',
                window, step):
            u = _user_for_pod(metric.get('pod', ''), names)
            if u:
                mem_by_user[u] = vals
        for metric, val in prom_instant_multi(
                f'max by (persistentvolumeclaim) (kubelet_volume_stats_used_bytes{{namespace="{ns}",persistentvolumeclaim=~"ws-.*-home"}})'):
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
        self.wfile.write(body)

    def read_json_body(self):
        n = int(self.headers.get('Content-Length', 0))
        if n == 0:
            return {}
        if n > MAX_REQUEST_BODY_BYTES:
            raise ValueError('request body too large')
        raw = self.rfile.read(n).decode('utf-8')
        return json.loads(raw) if raw else {}

    def check_admin(self):
        """True if the request is from an allowed admin.

        oauth2-proxy is the primary gate; this is defense-in-depth. The local
        dev bearer token (CONTROLLER_DEV_TOKEN, never set in-cluster) is the
        only path that doesn't require proxy headers.
        """
        if DEV_TOKEN:
            auth = self.headers.get('Authorization', '')
            if auth.startswith('Bearer ') and hmac.compare_digest(auth[7:].strip(), DEV_TOKEN):
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
            if user or email:
                if ADMIN_USERS and user and user.lower() not in ADMIN_USERS:
                    return False  # authenticated, but not on the allowlist
                return True
        return False

    def _norm_path(self):
        """Strip query + the SPA's /oauth prefix; return the upstream path."""
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith('/oauth/'):
            path = path[len('/oauth'):]   # '/oauth/api/x' -> '/api/x'
        elif path == '/oauth':
            path = '/'
        return path

    # ----- routing -----

    def do_GET(self):
        path = self._norm_path()
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
                self.send_json(list_workspaces())
            except KubectlError as exc:
                self.send_json({'error': str(exc), 'detail': exc.stderr}, 502)
            return
        if path == '/api/insights':
            if not self.check_admin():
                self.send_json({'error': 'unauthorized'}, 401)
                return
            try:
                self.send_json(compute_insights())
            except KubectlError as exc:
                self.send_json({'error': str(exc), 'detail': exc.stderr}, 502)
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
                self.send_json({'error': str(exc), 'detail': exc.stderr}, 502)
            return
        if path.startswith('/api/'):
            self.send_json({'error': 'not found'}, 404)
            return
        self.serve_spa(path)

    def do_POST(self):
        path = self._norm_path()
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
            self.send_json({'error': str(exc), 'detail': exc.stderr}, 502)
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
    httpd = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f'[controller] listening on 0.0.0.0:{PORT}', file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == '__main__':
    main()
